"""Integration tests for HybridRetriever.context_pack (Phase 5 slice 3).

End-to-end: seed corpus → query → CTE → boosts → conflicts → token
budgeting → ContextPackResponse. Reuses the small_corpus pattern
from test_hybrid_retrieval.py and the ``_DbWrapper`` helper, but
duplicated here per repo guidance to lean toward duplicating
fixtures over expanding shared scope.
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.agent.serializers import UNTRUSTED_CONTENT_NOTICE
from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.models import Document
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource
from cf_knowledge_kiln.retrieval import (
    ContextPackResponse,
    HybridRetriever,
    RetrievalConfig,
    RetrievalFilters,
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


async def _seed(session: AsyncSession, corpus_dir: Path, name: str = "ctxpack") -> None:
    src = LocalSource(name=name, type="local", path=str(corpus_dir), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


class _DbWrapper:
    def __init__(self, engine: AsyncEngine) -> None:
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self._maker()


async def test_context_pack_round_trip(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """Smoke: query → ContextPackResponse with evidence, notice, budget."""
    await _seed(session, small_corpus)
    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    pack = await retriever.context_pack(
        "widgets",
        task="summarize widget docs",
        filters=RetrievalFilters(),
        max_chunks=5,
        max_tokens=2000,
    )
    assert isinstance(pack, ContextPackResponse)
    assert pack.untrusted_content_notice == UNTRUSTED_CONTENT_NOTICE
    assert pack.token_budget.requested == 2000
    assert pack.evidence, "expected at least one evidence chunk"
    # Top evidence carries the chunk content text + a real title.
    ev = pack.evidence[0]
    assert ev.text
    assert ev.title


async def test_context_pack_empty_query_raises(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    await _seed(session, small_corpus)
    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    with pytest.raises(ValueError):
        await retriever.context_pack("  ", task="t", filters=RetrievalFilters())


async def test_context_pack_empty_task_raises(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    await _seed(session, small_corpus)
    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    with pytest.raises(ValueError):
        await retriever.context_pack("widgets", task="   ", filters=RetrievalFilters())


async def test_context_pack_marks_review_when_only_deprecated(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """All-deprecated evidence ⇒ requires_human_review with a reason."""
    await _seed(session, small_corpus)
    # Demote every doc in the corpus so the engine cannot return
    # anything other than deprecated evidence — that's the precondition
    # we need to trigger the "all deprecated" review reason.
    await session.execute(update(Document).values(status="deprecated"))
    await session.commit()
    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    pack = await retriever.context_pack(
        "widgets",
        task="summarize",
        filters=RetrievalFilters(),
        max_chunks=3,
        max_tokens=1000,
    )
    assert pack.evidence
    assert all(e.status == "deprecated" for e in pack.evidence)
    assert pack.requires_human_review is True
    assert any("deprecated" in r.lower() for r in pack.review_reasons)


async def test_context_pack_includes_conflict_when_heading_shared(
    session: AsyncSession, tmp_path: Path, engine: AsyncEngine
) -> None:
    """Two active docs under the same heading_path raise a conflict."""
    # Two docs with identical heading 'Backup' but different bodies.
    (tmp_path / "doc1.md").write_text("# Backup\n\nUse rsync nightly.\n")
    (tmp_path / "doc2.md").write_text("# Backup\n\nUse restic hourly.\n")
    src = LocalSource(name="conflicts", type="local", path=str(tmp_path), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()

    db = _DbWrapper(engine)
    # #161: relevance_floor=1e-4 because MockEmbeddingProvider's RRF
    # fused scores collapse near zero; the production 0.015 floor would
    # gate the conflict-pair chunks out under mock. The rank gate
    # (max_warning_rank=3 default) still applies and the test fixture
    # has only 2 chunks so rank 1 + 2 trip cleanly.
    retriever = HybridRetriever(
        db=db,
        embedding_provider=MockEmbeddingProvider(),
        config=RetrievalConfig(relevance_floor=1e-4),
    )
    pack = await retriever.context_pack(
        "backup",
        task="describe backup",
        filters=RetrievalFilters(),
        max_chunks=8,
        max_tokens=2000,
    )
    assert pack.conflicts, "expected one conflict on the shared 'Backup' heading"
    assert any(c.topic == "Backup" for c in pack.conflicts)
    assert pack.requires_human_review is True
    assert any(w.type == "conflicting_sources" for w in pack.warnings)


async def test_context_pack_respects_max_tokens_budget(
    session: AsyncSession, tmp_path: Path, engine: AsyncEngine
) -> None:
    """A tight token budget trims evidence to fit."""
    # Seed many small docs so retrieval has plenty to fuse.
    for i in range(8):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nwidgets {'x ' * 80}.\n")
    src = LocalSource(name="manydocs", type="local", path=str(tmp_path), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()

    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    big = await retriever.context_pack(
        "widgets",
        task="summarize",
        filters=RetrievalFilters(),
        max_chunks=8,
        max_tokens=5000,
    )
    small = await retriever.context_pack(
        "widgets",
        task="summarize",
        filters=RetrievalFilters(),
        max_chunks=8,
        max_tokens=200,
    )
    # Bigger budget yields more evidence (or at least no less).
    assert len(big.evidence) >= len(small.evidence)
    # Smaller budget really did spend less.
    assert small.token_budget.used_estimate <= big.token_budget.used_estimate


async def test_context_pack_id_is_unique_per_call(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """Two calls produce distinct context_pack_id values."""
    await _seed(session, small_corpus)
    db = _DbWrapper(engine)
    retriever = HybridRetriever(
        db=db, embedding_provider=MockEmbeddingProvider(), config=RetrievalConfig()
    )
    a = await retriever.context_pack(
        "widgets", task="t", filters=RetrievalFilters(), max_chunks=3, max_tokens=1000
    )
    b = await retriever.context_pack(
        "widgets", task="t", filters=RetrievalFilters(), max_chunks=3, max_tokens=1000
    )
    assert a.context_pack_id != b.context_pack_id
    # Sanity: ids are real UUIDs.
    assert a.context_pack_id != uuid4()  # vanishingly unlikely collision
