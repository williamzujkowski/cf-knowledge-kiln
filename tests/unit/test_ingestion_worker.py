"""Unit tests for the ingestion worker scaffolding (#46).

The DB-touching parts of :class:`Worker` (claim / process / mark_done /
recovery sweep) live in ``tests/integration/test_ingestion_worker.py``
where a real pgvector Postgres is available. This file covers the
parts that don't need a database:

* ``_build_provider_or_warn`` policy: missing config = warn + None,
  malformed config = raise, valid config = real provider.
* ``serve()`` early-exit paths: malformed allowlist, missing DB URL.
* ``Worker._process`` payload validation.
* ``Worker.request_shutdown`` idempotency.
* ``Worker`` constructor poll-interval override.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.ingestion import worker as worker_mod
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.factory import EmbeddingConfigError
from cf_knowledge_kiln.ingestion.sources import SourceAllowlist
from cf_knowledge_kiln.ingestion.worker import Worker, _build_provider_or_warn, serve


def _settings(**overrides: object) -> Settings:
    """Settings instance that ignores the on-disk .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type, call-arg]


def _write_models_yaml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def empty_allowlist() -> SourceAllowlist:
    return SourceAllowlist(sources=[])


class TestBuildProviderOrWarn:
    def test_missing_config_returns_none_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _settings(models_config_path=str(tmp_path / "absent.yaml"))
        with caplog.at_level(logging.WARNING):
            result = _build_provider_or_warn(settings)
        assert result is None
        assert any("no embedding config" in r.getMessage() for r in caplog.records)

    def test_excluded_model_raises_at_startup(self, tmp_path: Path) -> None:
        path = _write_models_yaml(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: local
    name: Qwen/Qwen3-Embedding-8B
    dimensions: 1024
    enabled: true
""",
        )
        settings = _settings(models_config_path=str(path))
        with pytest.raises(EmbeddingConfigError, match="excluded"):
            _build_provider_or_warn(settings)

    def test_valid_mock_config_returns_provider(self, tmp_path: Path) -> None:
        path = _write_models_yaml(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: true
""",
        )
        settings = _settings(models_config_path=str(path))
        provider = _build_provider_or_warn(settings)
        assert isinstance(provider, MockEmbeddingProvider)
        assert provider.dimensions == 768

    def test_disabled_model_is_fatal_at_startup(self, tmp_path: Path) -> None:
        """An operator who wrote enabled=false in production wants to know."""
        path = _write_models_yaml(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: false
""",
        )
        settings = _settings(models_config_path=str(path))
        with pytest.raises(EmbeddingConfigError, match="disabled"):
            _build_provider_or_warn(settings)


class TestServeEarlyExits:
    async def test_malformed_allowlist_returns_exit_code_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "sources.yaml"
        bad.write_text(":\n  -not: valid: yaml\n", encoding="utf-8")
        code = await serve(allowlist_path=bad, settings=_settings())
        assert code == 2

    async def test_missing_database_url_returns_exit_code_2(self, tmp_path: Path) -> None:
        good = tmp_path / "sources.yaml"
        good.write_text("sources: []\n", encoding="utf-8")
        settings = _settings(database_url=None)
        code = await serve(allowlist_path=good, settings=settings)
        assert code == 2


class TestWorkerProcessPayloadValidation:
    async def test_missing_source_name_raises_value_error(
        self, empty_allowlist: SourceAllowlist
    ) -> None:
        """Worker._process is the boundary between queue payload and code."""
        worker = Worker(
            db=_NullDatabase(),  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        with pytest.raises(ValueError, match="source_name"):
            await worker._process("job-id", {})  # type: ignore[arg-type]

    async def test_unknown_source_name_propagates(self, empty_allowlist: SourceAllowlist) -> None:
        worker = Worker(
            db=_NullDatabase(),  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        # Empty allowlist refuses every name; the resulting exception is
        # what `_tick` catches and converts into `mark_failed`.
        from cf_knowledge_kiln.ingestion.sources import SourceNotAllowedError

        with pytest.raises(SourceNotAllowedError):
            await worker._process("job-id", {"source_name": "nope"})


class TestWorkerShutdownAndPolling:
    def test_request_shutdown_is_idempotent(self, empty_allowlist: SourceAllowlist) -> None:
        worker = Worker(
            db=_NullDatabase(),  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        worker.request_shutdown()
        worker.request_shutdown()  # second call must not raise
        assert worker._shutdown.is_set()

    def test_poll_interval_override_wins_over_settings(
        self, empty_allowlist: SourceAllowlist
    ) -> None:
        worker = Worker(
            db=_NullDatabase(),  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(ingest_poll_interval_seconds=99.0),
            poll_interval_seconds=0.25,
        )
        assert worker._poll == 0.25

    def test_poll_interval_defaults_to_settings(self, empty_allowlist: SourceAllowlist) -> None:
        worker = Worker(
            db=_NullDatabase(),  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(ingest_poll_interval_seconds=7.5),
        )
        assert worker._poll == 7.5


class TestWorkerRunForeverShutdownSemantics:
    async def test_pre_set_shutdown_exits_after_one_recovery_sweep(
        self,
        empty_allowlist: SourceAllowlist,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If shutdown is set before run_forever, exit after the sweep.

        Without this, a fast test couldn't observe the loop terminating
        without standing up the queue + DB.
        """
        worker = Worker(
            db=_NullDatabase(),  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
            poll_interval_seconds=0.05,
        )
        sweep_calls = 0

        async def fake_sweep() -> None:
            nonlocal sweep_calls
            sweep_calls += 1

        async def fake_tick() -> bool:
            return False

        monkeypatch.setattr(worker, "_recover_stale_running", fake_sweep)
        monkeypatch.setattr(worker, "_tick", fake_tick)
        worker.request_shutdown()
        await asyncio.wait_for(worker.run_forever(), timeout=1.0)
        assert sweep_calls == 1


class _NullDatabase:
    """Stand-in Database that never opens a session.

    Used by tests that only exercise validation paths that raise
    *before* the worker reaches into the database.
    """

    def session(self) -> Iterator[Any]:  # pragma: no cover — never called
        raise AssertionError("test reached db.session(); add a real fake")

    async def dispose(self) -> None:  # pragma: no cover
        return None

    async def ping(self) -> bool:  # pragma: no cover
        return True


class TestRecoverStaleRunningUsesRepo:
    """Verify the recovery sweep funnels through the repo, not raw SQL.

    Pure logic check via monkeypatching the repository class on the
    worker module; no DB needed.
    """

    async def test_no_stale_rows_logs_nothing(
        self, empty_allowlist: SourceAllowlist, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_session = _FakeAsyncContext()
        fake_db = _SingleSessionDatabase(fake_session)
        fake_repo = AsyncMock()
        fake_repo.list = AsyncMock(return_value=[])
        fake_repo.requeue = AsyncMock()
        monkeypatch.setattr(worker_mod, "IngestionJobsRepository", lambda _session: fake_repo)
        worker = Worker(
            db=fake_db,  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        await worker._recover_stale_running()
        fake_repo.requeue.assert_not_called()

    async def test_stale_rows_get_requeued_one_by_one(
        self, empty_allowlist: SourceAllowlist, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_session = _FakeAsyncContext()
        fake_db = _SingleSessionDatabase(fake_session)
        stale_jobs = [_StubJob("a"), _StubJob("b"), _StubJob("c")]
        fake_repo = AsyncMock()
        fake_repo.list = AsyncMock(return_value=stale_jobs)
        fake_repo.requeue = AsyncMock()
        monkeypatch.setattr(worker_mod, "IngestionJobsRepository", lambda _session: fake_repo)
        worker = Worker(
            db=fake_db,  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        await worker._recover_stale_running()
        assert fake_repo.requeue.await_count == 3
        assert [c.args[0] for c in fake_repo.requeue.await_args_list] == ["a", "b", "c"]


class _StubJob:
    def __init__(self, id: str) -> None:
        self.id = id


class _FakeAsyncContext:
    """Async context manager that yields itself; used in place of an AsyncSession."""

    async def __aenter__(self) -> _FakeAsyncContext:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _SingleSessionDatabase:
    """Stand-in Database that always returns the same fake session."""

    def __init__(self, session: _FakeAsyncContext) -> None:
        self._session = session

    def session(self) -> _FakeAsyncContext:
        return self._session

    async def dispose(self) -> None:  # pragma: no cover
        return None

    async def ping(self) -> bool:  # pragma: no cover
        return True
