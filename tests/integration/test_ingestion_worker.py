"""Integration tests for the worker's claim → process → mark-done loop (#46).

Companion to ``tests/unit/test_ingestion_worker.py``. These tests need
a real database because they exercise the queue + the ingestion
pipeline together; the unit suite covers the parts that can be
asserted without one.
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db import Database, resolve_database_url
from cf_knowledge_kiln.db.models import IngestionJob, IngestionRun
from cf_knowledge_kiln.db.repositories import IngestionJobsRepository
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.sources import (
    LocalSource,
    SourceAllowlist,
)
from cf_knowledge_kiln.ingestion.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def fixture_corpus(tmp_path: Path) -> Path:
    """Two markdown files; small, enough to exercise the pipeline."""
    (tmp_path / "intro.md").write_text(
        textwrap.dedent(
            """\
            # Intro
            Welcome to the test corpus.
            """
        )
    )
    (tmp_path / "topic.md").write_text("# Topic\n\nBody.\n")
    return tmp_path


@pytest.fixture
def allowlist(fixture_corpus: Path) -> SourceAllowlist:
    src = LocalSource(
        name="fixtures",
        type="local",
        path=str(fixture_corpus),
        include=["**/*.md"],
    )
    return SourceAllowlist(sources=[src])


@pytest_asyncio.fixture
async def database(database_url: str) -> AsyncIterator[Database]:
    db = Database(database_url, pool_size=2, max_overflow=2)
    yield db
    await db.dispose()


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=100,
        ingest_max_repo_bytes=10 * 1_048_576,
    )


async def _enqueue(db: Database, source_name: str) -> str:
    """Insert one queued job and commit; return its id."""
    async with db.session() as session:
        job = await IngestionJobsRepository(session).create(
            kind="full_resync", payload={"source_name": source_name}
        )
        await session.commit()
        return str(job.id)


async def test_worker_tick_claims_and_processes_a_job(
    database: Database,
    allowlist: SourceAllowlist,
    engine: AsyncEngine,
) -> None:
    """One queued job → one ingestion_runs row + job moves to succeeded."""
    await _enqueue(database, "fixtures")
    worker = Worker(
        db=database,
        allowlist=allowlist,
        settings=_settings(),
        embedding_provider=MockEmbeddingProvider(),
        poll_interval_seconds=0.01,
    )
    processed = await worker._tick()
    assert processed is True

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        runs = (await session.execute(select(IngestionRun))).scalars().all()
        jobs = (await session.execute(select(IngestionJob))).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert len(jobs) == 1
    assert jobs[0].status == "succeeded"
    assert jobs[0].finished_at is not None


async def test_worker_tick_marks_failed_when_source_name_unknown(
    database: Database,
    allowlist: SourceAllowlist,
    engine: AsyncEngine,
) -> None:
    """An unknown source_name in the payload becomes a `failed` job, not a crash."""
    await _enqueue(database, "not-in-allowlist")
    worker = Worker(
        db=database,
        allowlist=allowlist,
        settings=_settings(),
        poll_interval_seconds=0.01,
    )
    processed = await worker._tick()
    assert processed is True

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        jobs = (await session.execute(select(IngestionJob))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert jobs[0].last_error and "not-in-allowlist" in jobs[0].last_error


async def test_worker_tick_returns_false_on_empty_queue(
    database: Database,
    allowlist: SourceAllowlist,
) -> None:
    worker = Worker(
        db=database,
        allowlist=allowlist,
        settings=_settings(),
        poll_interval_seconds=0.01,
    )
    assert await worker._tick() is False


async def test_recover_stale_running_requeues_orphans(
    database: Database,
    allowlist: SourceAllowlist,
    engine: AsyncEngine,
) -> None:
    """A row left in `running` by a prior crashed process gets requeued."""
    # Simulate a crashed worker: insert a job and immediately claim it,
    # which transitions it to `running`. Don't commit anything else.
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        repo = IngestionJobsRepository(session)
        await repo.create(payload={"source_name": "fixtures"})
        await session.commit()
    async with maker() as session:
        await IngestionJobsRepository(session).claim_one()
        await session.commit()
    async with maker() as session:
        jobs = (await session.execute(select(IngestionJob))).scalars().all()
        assert {j.status for j in jobs} == {"running"}

    worker = Worker(db=database, allowlist=allowlist, settings=_settings())
    await worker._recover_stale_running()

    async with maker() as session:
        jobs = (await session.execute(select(IngestionJob))).scalars().all()
    assert {j.status for j in jobs} == {"queued"}


async def test_serve_resolves_database_url_when_set(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    """resolve_database_url must accept the integration DSN.

    Lightweight sanity check that the worker module's URL plumbing is
    consistent with the integration fixture; if this ever breaks, the
    other worker integration tests stop being meaningful.
    """
    monkeypatch.setenv("KILN_DATABASE_URL", database_url)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert resolve_database_url(settings) == database_url
