"""Unit tests for the ingestion worker scaffolding (#46).

The DB-touching parts of :class:`Worker` (claim / process / mark_done /
recovery sweep) live in ``tests/integration/test_ingestion_worker.py``
where a real pgvector Postgres is available. This file covers the
parts that don't need a database:

* ``serve()`` early-exit paths: malformed allowlist, missing DB URL.
* ``Worker._process`` payload validation.
* ``Worker.request_shutdown`` idempotency.
* ``Worker`` constructor poll-interval override.

Embedding-provider construction policy used to live in this file; it
moved to ``tests/unit/test_ingestion_embedding_factory.py`` once the
factory function was hoisted out of ``worker.py`` (issue #58).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.ingestion import worker as worker_mod
from cf_knowledge_kiln.ingestion.sources import SourceAllowlist
from cf_knowledge_kiln.ingestion.worker import Worker, serve


def _settings(**overrides: object) -> Settings:
    """Settings instance that ignores the on-disk .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type, call-arg]


@pytest.fixture
def empty_allowlist() -> SourceAllowlist:
    return SourceAllowlist(sources=[])


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
        """Rows without a result_run_id are requeued (work didn't finish)."""
        fake_session = _FakeAsyncContext()
        fake_db = _SingleSessionDatabase(fake_session)
        stale_jobs = [_StubJob("a"), _StubJob("b"), _StubJob("c")]
        fake_repo = AsyncMock()
        fake_repo.list = AsyncMock(return_value=stale_jobs)
        fake_repo.requeue = AsyncMock()
        fake_repo.mark_done = AsyncMock()
        monkeypatch.setattr(worker_mod, "IngestionJobsRepository", lambda _session: fake_repo)
        worker = Worker(
            db=fake_db,  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        await worker._recover_stale_running()
        assert fake_repo.requeue.await_count == 3
        assert [c.args[0] for c in fake_repo.requeue.await_args_list] == ["a", "b", "c"]
        fake_repo.mark_done.assert_not_called()

    async def test_stale_row_with_succeeded_run_gets_marked_done_not_requeued(
        self, empty_allowlist: SourceAllowlist, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#47: a crash AFTER run_source committed but BEFORE mark_done
        leaves a `running` job whose result_run_id points at a succeeded
        run. The recovery sweep recognizes this and marks the job done
        instead of re-doing the work.
        """
        from uuid import uuid4

        run_id = uuid4()
        # Session needs to answer .get(IngestionRun, run_id) with a
        # succeeded run.
        fake_session = _FakeAsyncContext()
        fake_run = type("FakeRun", (), {"status": "succeeded"})()
        fake_session.get = AsyncMock(return_value=fake_run)  # type: ignore[attr-defined]
        fake_db = _SingleSessionDatabase(fake_session)
        stale_jobs = [_StubJob("a", result_run_id=run_id)]
        fake_repo = AsyncMock()
        fake_repo.list = AsyncMock(return_value=stale_jobs)
        fake_repo.requeue = AsyncMock()
        fake_repo.mark_done = AsyncMock()
        monkeypatch.setattr(worker_mod, "IngestionJobsRepository", lambda _session: fake_repo)
        worker = Worker(
            db=fake_db,  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        await worker._recover_stale_running()
        fake_repo.requeue.assert_not_called()
        fake_repo.mark_done.assert_awaited_once_with("a", result_run_id=run_id)

    async def test_stale_row_with_failed_run_gets_requeued(
        self, empty_allowlist: SourceAllowlist, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run that's still `running` / `failed` → requeue (work didn't durably persist)."""
        from uuid import uuid4

        run_id = uuid4()
        fake_session = _FakeAsyncContext()
        fake_run = type("FakeRun", (), {"status": "failed"})()
        fake_session.get = AsyncMock(return_value=fake_run)  # type: ignore[attr-defined]
        fake_db = _SingleSessionDatabase(fake_session)
        stale_jobs = [_StubJob("a", result_run_id=run_id)]
        fake_repo = AsyncMock()
        fake_repo.list = AsyncMock(return_value=stale_jobs)
        fake_repo.requeue = AsyncMock()
        fake_repo.mark_done = AsyncMock()
        monkeypatch.setattr(worker_mod, "IngestionJobsRepository", lambda _session: fake_repo)
        worker = Worker(
            db=fake_db,  # type: ignore[arg-type]
            allowlist=empty_allowlist,
            settings=_settings(),
        )
        await worker._recover_stale_running()
        fake_repo.requeue.assert_awaited_once_with("a")
        fake_repo.mark_done.assert_not_called()


class _StubJob:
    """Minimal IngestionJob stand-in for the recovery-sweep tests."""

    def __init__(self, id: str, *, result_run_id: object | None = None) -> None:
        self.id = id
        self.result_run_id = result_run_id


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
