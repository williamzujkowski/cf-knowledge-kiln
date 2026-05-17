"""End-to-end ingestion pipeline tests (#13/#14/#15/#40).

Builds a local-source fixture directory with three Markdown files,
runs :func:`pipeline.run_source` against it, and asserts:

* a `data_sources` row is created,
* `documents` rows are upserted per file,
* `document_chunks` carry heading_path + content_hash,
* an `ingestion_runs` summary lands with `succeeded` status,
* a second run with unchanged content does no chunk-creation work,
* the IngestionJobsRepository.claim_one round-trip works under
  SKIP LOCKED.
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.models import (
    ChunkEmbedding,
    DataSource,
    Document,
    DocumentChunk,
    IngestionRun,
)
from cf_knowledge_kiln.db.repositories import IngestionJobsRepository
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource


class _CountingMockProvider(MockEmbeddingProvider):
    """Mock provider that records every ``embed`` call for assertions."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return await super().embed(texts)


class _RaisingMockProvider(MockEmbeddingProvider):
    """Mock provider whose embed() always raises; for failure-path tests."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("simulated provider outage")


class _WrongLengthMockProvider(MockEmbeddingProvider):
    """Mock provider that returns the wrong number of vectors."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Return one fewer vector than requested.
        truncated = texts[:-1] if len(texts) > 1 else []
        return await super().embed(truncated)


pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def fixture_corpus(tmp_path: Path) -> Path:
    """Three markdown files under a local-source root."""
    (tmp_path / "intro.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Intro
            owner: platform
            ---
            # Intro
            Welcome.
            """
        )
    )
    (tmp_path / "guide.md").write_text("# Guide\n\nContents.\n")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "topic.md").write_text("# Topic\n\nBody.\n")
    return tmp_path


def _settings() -> Settings:
    return Settings(
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=100,
        ingest_max_repo_bytes=10 * 1_048_576,
    )


async def test_pipeline_indexes_local_source_end_to_end(
    session: AsyncSession, fixture_corpus: Path
) -> None:
    src = LocalSource(name="fixtures", type="local", path=str(fixture_corpus), include=["**/*.md"])
    summary = await run_source(session, source=src, settings=_settings())
    await session.commit()
    assert summary.files_indexed == 3
    assert summary.chunks_created >= 3

    docs = (await session.execute(select(Document))).scalars().all()
    assert {d.path for d in docs} == {"intro.md", "guide.md", "deep/topic.md"}
    intro = next(d for d in docs if d.path == "intro.md")
    assert intro.title == "Intro"
    assert intro.owner == "platform"

    chunks = (await session.execute(select(DocumentChunk))).scalars().all()
    assert all(c.content_hash.startswith("sha256:") for c in chunks)
    assert any(c.heading_path == ["Intro"] for c in chunks)

    runs = (await session.execute(select(IngestionRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert runs[0].stats["files_indexed"] == 3

    sources = (await session.execute(select(DataSource))).scalars().all()
    assert {s.name for s in sources} == {"fixtures"}


async def test_pipeline_is_idempotent_on_unchanged_content(
    session: AsyncSession, fixture_corpus: Path
) -> None:
    src = LocalSource(name="fixtures", type="local", path=str(fixture_corpus), include=["**/*.md"])
    first = await run_source(session, source=src, settings=_settings())
    await session.commit()
    second = await run_source(session, source=src, settings=_settings())
    await session.commit()
    assert second.chunks_created == 0
    assert second.chunks_unchanged == first.chunks_created
    assert second.files_indexed == first.files_indexed


async def test_ingestion_jobs_claim_one_is_safe_under_skip_locked(
    engine: AsyncEngine,
) -> None:
    """Two concurrent claim_one calls don't both grab the same row."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # Enqueue 2 jobs.
    async with maker() as s:
        jobs = IngestionJobsRepository(s)
        await jobs.create(payload={"source_name": "a"})
        await jobs.create(payload={"source_name": "b"})
        await s.commit()

    # Two open sessions; the second claim must skip the row held by the first.
    async with maker() as s1, maker() as s2:
        claim_a = await IngestionJobsRepository(s1).claim_one()
        claim_b = await IngestionJobsRepository(s2).claim_one()
        assert claim_a is not None
        assert claim_b is not None
        assert claim_a.id != claim_b.id
        await s1.commit()
        await s2.commit()


async def test_pipeline_aborts_on_repo_cap_and_marks_run(
    session: AsyncSession, tmp_path: Path
) -> None:
    """When the repo cap trips, ingestion_runs records failed/partial status."""
    for i in range(5):
        (tmp_path / f"f{i}.md").write_text("x" * 1000)
    src = LocalSource(name="tinycap", type="local", path=str(tmp_path), include=["**/*.md"])
    settings = Settings(
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=100,
        ingest_max_repo_bytes=2000,  # too small
    )
    summary = await run_source(session, source=src, settings=settings)
    await session.commit()
    assert summary.errors
    runs = (await session.execute(select(IngestionRun))).scalars().all()
    assert any(r.status == "failed" for r in runs)


async def test_pipeline_embeds_new_chunks_when_provider_supplied(
    session: AsyncSession, fixture_corpus: Path
) -> None:
    """Issue #18: every new chunk gets exactly one embedding row."""
    provider = _CountingMockProvider()
    src = LocalSource(name="fixtures", type="local", path=str(fixture_corpus), include=["**/*.md"])
    summary = await run_source(
        session, source=src, settings=_settings(), embedding_provider=provider
    )
    await session.commit()
    assert summary.embeddings_created == summary.chunks_created
    assert summary.embeddings_created > 0
    embeddings = (await session.execute(select(ChunkEmbedding))).scalars().all()
    assert len(embeddings) == summary.chunks_created
    assert all(e.provider == "mock" for e in embeddings)
    assert all(e.dimensions == 768 for e in embeddings)


async def test_pipeline_skips_embeddings_on_unchanged_re_ingest(
    session: AsyncSession, fixture_corpus: Path
) -> None:
    """Issue #18 acceptance: re-ingestion of unchanged corpus = zero calls."""
    provider = _CountingMockProvider()
    src = LocalSource(name="fixtures", type="local", path=str(fixture_corpus), include=["**/*.md"])

    first = await run_source(session, source=src, settings=_settings(), embedding_provider=provider)
    await session.commit()
    calls_after_first = len(provider.calls)
    assert first.embeddings_created > 0

    second = await run_source(
        session, source=src, settings=_settings(), embedding_provider=provider
    )
    await session.commit()
    # Re-ingest of identical content: provider must not be called again.
    assert len(provider.calls) == calls_after_first
    assert second.embeddings_created == 0
    assert second.embeddings_unchanged == first.embeddings_created


async def test_pipeline_re_embeds_only_changed_chunks(
    session: AsyncSession, tmp_path: Path
) -> None:
    """A chunk whose hash changed gets one new embedding; siblings stay put."""
    (tmp_path / "doc.md").write_text("# Doc\n\noriginal body\n")
    src = LocalSource(name="changing", type="local", path=str(tmp_path), include=["**/*.md"])
    provider = _CountingMockProvider()

    first = await run_source(session, source=src, settings=_settings(), embedding_provider=provider)
    await session.commit()
    initial_call_count = len(provider.calls)
    assert first.embeddings_created >= 1

    # Edit the file → new chunk hash for the changed chunk.
    (tmp_path / "doc.md").write_text("# Doc\n\nedited body\n")
    second = await run_source(
        session, source=src, settings=_settings(), embedding_provider=provider
    )
    await session.commit()
    # At least one fresh embedding call for the changed chunk.
    assert len(provider.calls) > initial_call_count
    assert second.embeddings_created >= 1


async def test_pipeline_records_embedding_provider_failure(
    session: AsyncSession, fixture_corpus: Path
) -> None:
    """Issue #46: provider exception → embeddings_failed counted, run survives."""
    provider = _RaisingMockProvider()
    src = LocalSource(name="fixtures", type="local", path=str(fixture_corpus), include=["**/*.md"])
    summary = await run_source(
        session, source=src, settings=_settings(), embedding_provider=provider
    )
    await session.commit()
    # Chunk pass still succeeded; embedding pass failed per-document.
    assert summary.chunks_created > 0
    assert summary.embeddings_created == 0
    assert summary.embeddings_failed == summary.chunks_created
    assert any("simulated provider outage" in e for e in summary.errors)
    runs = (await session.execute(select(IngestionRun))).scalars().all()
    # Status is "partial" because errors were recorded, not "failed".
    assert runs[0].status == "partial"


async def test_pipeline_records_vector_count_mismatch(
    session: AsyncSession, fixture_corpus: Path
) -> None:
    """Issue #46: provider returns wrong number of vectors → failure counted."""
    provider = _WrongLengthMockProvider()
    src = LocalSource(name="fixtures", type="local", path=str(fixture_corpus), include=["**/*.md"])
    summary = await run_source(
        session, source=src, settings=_settings(), embedding_provider=provider
    )
    await session.commit()
    assert summary.embeddings_created == 0
    assert summary.embeddings_failed > 0
    assert any("vectors for" in e for e in summary.errors)


async def test_pipeline_handles_document_with_zero_chunks(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Issue #46: an empty Markdown file produces no chunks and no errors.

    Parser today treats an effectively-empty body as 'no chunks' and the
    file is recorded as files_skipped + a warning. The embedding pass
    must then see no chunks and short-circuit.
    """
    (tmp_path / "empty.md").write_text("")
    src = LocalSource(name="empties", type="local", path=str(tmp_path), include=["**/*.md"])
    provider = _CountingMockProvider()
    summary = await run_source(
        session, source=src, settings=_settings(), embedding_provider=provider
    )
    await session.commit()
    assert summary.embeddings_created == 0
    assert summary.embeddings_failed == 0
    assert provider.calls == []


async def test_pipeline_records_skip_reasons_in_run_stats(
    session: AsyncSession, tmp_path: Path
) -> None:
    (tmp_path / "ok.md").write_text("# Ok\nbody\n")
    (tmp_path / "ignored.png").write_text("xx")
    src = LocalSource(name="mixed", type="local", path=str(tmp_path), include=["**/*"])
    summary = await run_source(session, source=src, settings=_settings())
    await session.commit()
    assert summary.skip_reasons.get("unsupported_file_type", 0) >= 1
    # Run's stats blob round-trips the skip_reasons map.
    last_run = (
        await session.execute(text("SELECT stats FROM ingestion_runs ORDER BY started_at DESC"))
    ).scalar_one()
    assert last_run["skip_reasons"]["unsupported_file_type"] >= 1
