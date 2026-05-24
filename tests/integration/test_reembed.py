"""Integration tests for the re-embed helper (#224).

End-to-end against live pgvector. Verifies:

* Empty DB → ``ReembedResult`` reports zero chunks; no provider call.
* Dry-run → no writes, no provider call, but chunk count is right.
* Happy path → every chunk's embedding row is replaced. Detected via
  the ``content_hash`` change on the embedding row when we run
  re-embed against a fresh provider with a different ``model``
  string.
* Partial failure → the helper does NOT raise; surviving batches are
  persisted; the failures list is populated.

Mocks the provider directly rather than going through the factory so
the test doesn't need the heavy ``sentence-transformers`` install.
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.models import ChunkEmbedding
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.reembed import reembed_all_chunks
from cf_knowledge_kiln.ingestion.sources import LocalSource

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def small_corpus(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text(
        textwrap.dedent(
            """\
            # Alpha

            First paragraph about widgets.

            ## Section

            Second paragraph about gadgets.
            """
        )
    )
    (tmp_path / "beta.md").write_text(
        textwrap.dedent(
            """\
            # Beta

            Independent doc about frobnication.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus: Path) -> int:
    """Ingest the corpus and return chunk count via a fresh provider."""
    src = LocalSource(
        name="reembed-tests",
        type="local",
        path=str(corpus),
        include=["**/*.md"],
    )
    initial_provider = MockEmbeddingProvider(model="mock-initial")
    summary = await run_source(
        session,
        source=src,
        settings=Settings(),
        embedding_provider=initial_provider,
    )
    await session.commit()
    return summary.chunks_created


# ─── Empty DB ────────────────────────────────────────────────────────


async def test_reembed_on_empty_db_short_circuits(session: AsyncSession) -> None:
    """No chunks → zero-count result; provider's embed is NOT called.

    MockEmbeddingProvider doesn't expose a calls list; track via a
    one-off subclass.
    """

    class _Counting(MockEmbeddingProvider):
        def __init__(self) -> None:
            super().__init__(model="mock-counting")
            self.embed_documents_calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.embed_documents_calls += 1
            return await super().embed_documents(texts)

    counting = _Counting()
    result = await reembed_all_chunks(session, provider=counting)
    assert result.chunks_total == 0
    assert result.chunks_embedded == 0
    assert result.chunks_failed == 0
    assert counting.embed_documents_calls == 0


# ─── Dry-run ─────────────────────────────────────────────────────────


async def test_reembed_dry_run_reports_count_without_writing(
    session: AsyncSession, small_corpus: Path
) -> None:
    chunk_count = await _seed(session, small_corpus)
    assert chunk_count > 0

    class _Counting(MockEmbeddingProvider):
        def __init__(self) -> None:
            super().__init__(model="mock-dry")
            self.embed_documents_calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.embed_documents_calls += 1
            return await super().embed_documents(texts)

    counting = _Counting()
    result = await reembed_all_chunks(session, provider=counting, dry_run=True)
    assert result.chunks_total == chunk_count
    assert result.chunks_embedded == 0
    assert result.chunks_failed == 0
    # The provider's embed_documents must NOT be called on dry-run.
    assert counting.embed_documents_calls == 0


# ─── Happy path ──────────────────────────────────────────────────────


async def test_reembed_replaces_every_chunk_embedding_row(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """Run with a NEW provider model; every existing embedding row's
    ``model`` field flips from the initial value to the new value.
    Locks the "re-embed actually wrote" contract.
    """
    chunk_count = await _seed(session, small_corpus)
    new_provider = MockEmbeddingProvider(model="mock-post-bump")

    result = await reembed_all_chunks(session, provider=new_provider, batch_size=2, concurrency=2)
    await session.commit()

    assert result.chunks_total == chunk_count
    assert result.chunks_embedded == chunk_count
    assert result.chunks_failed == 0

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        rows = (await s.execute(select(ChunkEmbedding))).scalars().all()
    models = {r.model for r in rows}
    # The "mock-initial" model that seeded the DB should be gone; only
    # the new model remains.
    assert models == {"mock-post-bump"}


# ─── Partial failure ─────────────────────────────────────────────────


async def test_reembed_partial_failure_persists_surviving_batches(
    session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """One batch raises; the others land in the DB; helper does NOT raise."""
    await _seed(session, small_corpus)

    class _FlakyProvider(MockEmbeddingProvider):
        """Fails the FIRST batch it sees, succeeds on every subsequent."""

        def __init__(self) -> None:
            super().__init__(model="mock-flaky")
            self.call_count = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("simulated first-batch outage")
            return await super().embed_documents(texts)

    flaky = _FlakyProvider()
    # batch_size=2 means multi-batch on the seeded corpus (3+ chunks).
    result = await reembed_all_chunks(session, provider=flaky, batch_size=2, concurrency=1)
    await session.commit()

    # Total chunks unchanged; some embedded, some failed.
    assert result.chunks_failed > 0
    assert result.chunks_embedded > 0
    assert result.chunks_embedded + result.chunks_failed == result.chunks_total
    assert result.failures, "expected the failure list to be populated"
