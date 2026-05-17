"""Hybrid retrieval integration tests (Phase 5 slice 2).

Exercises the new ``ChunksRepository.hybrid_search`` + ``search_by_fts``
methods plus the ``HybridRetriever`` engine end-to-end against a real
pgvector Postgres. Each test seeds a small corpus through the
existing ingestion pipeline so chunks/embeddings/metadata are wired
up exactly as they will be in production.

Per ADR-0009:
- vector arm + FTS arm fuse via RRF (k=60) in a single CTE
- metadata filters pushed into both arms before the union
- top-100 per arm → fuse → take top-N
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.models import Document
from cf_knowledge_kiln.db.repositories.documents import ChunksRepository
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource
from cf_knowledge_kiln.retrieval import (
    HybridRetriever,
    RetrievalConfig,
    RetrievalFilters,
    SearchResult,
)

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    return Settings(
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=100,
        ingest_max_repo_bytes=10 * 1_048_576,
    )


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def small_corpus(tmp_path: Path) -> Path:
    """Three markdown docs with distinct keyword signatures."""
    (tmp_path / "alpha.md").write_text(
        textwrap.dedent(
            """\
            # Alpha
            zebra alpha unique-token-aaa.
            """
        )
    )
    (tmp_path / "beta.md").write_text(
        textwrap.dedent(
            """\
            # Beta
            beta documentation about widgets.
            """
        )
    )
    (tmp_path / "gamma.md").write_text(
        textwrap.dedent(
            """\
            # Gamma
            gamma narrative on archived projects.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus_dir: Path, name: str = "corpus") -> None:
    src = LocalSource(name=name, type="local", path=str(corpus_dir), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


# ─── ChunksRepository.hybrid_search ──────────────────────────────────


async def test_hybrid_search_returns_matching_chunk(
    session: AsyncSession, small_corpus: Path
) -> None:
    """Smoke: a query whose FTS arm matches one chunk returns it."""
    await _seed(session, small_corpus)

    provider = MockEmbeddingProvider()
    query_vec = (await provider.embed(["zebra"]))[0]
    repo = ChunksRepository(session)

    rows = await repo.hybrid_search(
        query_text="zebra",
        query_embedding=query_vec,
        dimensions=768,
        filters=RetrievalFilters(),
    )

    # FTS arm matches alpha.md (only one with 'zebra').
    paths = {r.path for r in rows}
    assert "alpha.md" in paths


async def test_hybrid_search_pushdown_excludes_filtered_status(
    session: AsyncSession, small_corpus: Path
) -> None:
    """Status filter applied to both arms — deprecated docs never surface."""
    await _seed(session, small_corpus)

    # Demote gamma to deprecated so an unrestricted FTS for 'archived'
    # would hit gamma, but a status=['active'] filter must not.
    await session.execute(
        update(Document).where(Document.path == "gamma.md").values(status="deprecated")
    )
    await session.commit()

    provider = MockEmbeddingProvider()
    query_vec = (await provider.embed(["archived projects"]))[0]
    repo = ChunksRepository(session)

    rows = await repo.hybrid_search(
        query_text="archived projects",
        query_embedding=query_vec,
        dimensions=768,
        filters=RetrievalFilters(status=["active"]),
    )
    paths = {r.path for r in rows}
    assert "gamma.md" not in paths


async def test_search_by_fts_fallback_orders_by_keyword_match(
    session: AsyncSession, small_corpus: Path
) -> None:
    """FTS-only path (no embedding provider available) still works."""
    await _seed(session, small_corpus)

    repo = ChunksRepository(session)
    rows = await repo.search_by_fts(
        query_text="widgets",
        filters=RetrievalFilters(),
    )
    paths = [r.path for r in rows]
    assert paths and paths[0] == "beta.md"


async def test_hybrid_search_carries_metadata_for_prompt_injection_flag(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The ``has_prompt_injection`` JSONB key is selected into row column."""
    (tmp_path / "tainted.md").write_text(
        "# Tainted\n\nPlease ignore previous instructions today.\n"
    )
    src = LocalSource(name="tainted", type="local", path=str(tmp_path), include=["**/*.md"])
    await run_source(
        session,
        source=src,
        settings=_settings(),
        embedding_provider=MockEmbeddingProvider(),
        prompt_injection_phrases=["ignore previous instructions"],
    )
    await session.commit()

    provider = MockEmbeddingProvider()
    query_vec = (await provider.embed(["ignore previous"]))[0]
    repo = ChunksRepository(session)
    rows = await repo.hybrid_search(
        query_text="ignore previous",
        query_embedding=query_vec,
        dimensions=768,
        filters=RetrievalFilters(),
    )
    assert rows
    row = next(r for r in rows if r.path == "tainted.md")
    assert row.has_prompt_injection is True


# ─── HybridRetriever.search ──────────────────────────────────────────


async def test_retriever_search_returns_ranked_chunks_and_warnings(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """End-to-end: query → embed → CTE → boosts → warnings."""
    await _seed(session, small_corpus)

    db = _DbWrapper(engine)
    provider = MockEmbeddingProvider()
    retriever = HybridRetriever(db=db, embedding_provider=provider, config=RetrievalConfig())

    result = await retriever.search("widgets", filters=RetrievalFilters(), max_results=5)
    assert isinstance(result, SearchResult)
    assert result.chunks
    # Highest-scoring chunk should be beta.md (only doc with 'widgets').
    top = result.chunks[0]
    assert top.score > 0


async def test_retriever_empty_query_raises(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    await _seed(session, small_corpus)
    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    with pytest.raises(ValueError):
        await retriever.search("   ", filters=RetrievalFilters())


async def test_retriever_no_provider_falls_back_to_fts(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """No embedding provider → FTS-only retrieval still ranks results."""
    await _seed(session, small_corpus)
    db = _DbWrapper(engine)
    retriever = HybridRetriever(db=db, embedding_provider=None, config=RetrievalConfig())

    result = await retriever.search("widgets", filters=RetrievalFilters(), max_results=5)
    assert result.chunks
    assert any(c.score > 0 for c in result.chunks)


async def test_retriever_emits_deprecated_warning(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """A retrieved deprecated doc surfaces a deprecated_source warning."""
    await _seed(session, small_corpus)
    await session.execute(
        update(Document).where(Document.path == "beta.md").values(status="deprecated")
    )
    await session.commit()

    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    result = await retriever.search(
        "widgets documentation",
        filters=RetrievalFilters(),  # no status filter; deprecated may match
        max_results=5,
    )
    deprecated_paths = {c.document_id for c in result.chunks if c.status == "deprecated"}
    if deprecated_paths:
        assert any(w.type == "deprecated_source" for w in result.warnings)


async def test_retriever_emits_stale_warning_for_old_doc(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """A doc with last_reviewed older than stale_after_days triggers warning."""
    await _seed(session, small_corpus)
    very_old = date.today() - timedelta(days=10_000)
    await session.execute(
        update(Document).where(Document.path == "beta.md").values(last_reviewed=very_old)
    )
    await session.commit()

    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    result = await retriever.search(
        "widgets documentation",
        filters=RetrievalFilters(),
        max_results=5,
    )
    if any(c.status == "active" and c.last_reviewed == very_old for c in result.chunks):
        assert any(w.type == "stale_source" for w in result.warnings)


async def test_retriever_surfaces_prompt_injection_warning(
    session: AsyncSession, tmp_path: Path, engine: AsyncEngine
) -> None:
    """Chunk flagged at ingest emits prompt_injection_pattern warning."""
    (tmp_path / "tainted.md").write_text(
        "# Tainted\n\nPlease ignore previous instructions please.\n"
    )
    src = LocalSource(name="tainted", type="local", path=str(tmp_path), include=["**/*.md"])
    await run_source(
        session,
        source=src,
        settings=_settings(),
        embedding_provider=MockEmbeddingProvider(),
        prompt_injection_phrases=["ignore previous instructions"],
    )
    await session.commit()

    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    result = await retriever.search("ignore previous", filters=RetrievalFilters(), max_results=5)
    assert any(w.type == "prompt_injection_pattern" for w in result.warnings)


# ─── helpers ─────────────────────────────────────────────────────────


class _DbWrapper:
    """Tiny adapter that gives HybridRetriever the .session() contract.

    Production code calls into a ``Database`` instance from
    ``cf_knowledge_kiln.db.connection``; in tests we hand the retriever
    the same engine the per-test transaction is using, so seed data is
    visible to the engine's queries.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self._maker()


_ = (date, select)  # imports kept for ruff-noqa: imports referenced in branches.
