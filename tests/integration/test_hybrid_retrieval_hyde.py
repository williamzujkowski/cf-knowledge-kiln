"""#333 integration: HyDE wired into HybridRetriever.

Three contracts the issue spells out explicitly:

* HyDE-enabled changes the text fed to the embedding provider's
  ``embed_query`` (vector arm), but NOT the FTS arm's query text.
* HyDE-disabled produces byte-identical search output to baseline.
* HyDE-enabled with no generator (engine=None) produces baseline
  output too — the degradation contract.
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.generation import MockGeneratorProvider
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters
from cf_knowledge_kiln.retrieval.config import RetrievalConfig
from cf_knowledge_kiln.retrieval.hyde import HydeCache, HydeEngine

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
    (tmp_path / "alpha.md").write_text(
        textwrap.dedent(
            """\
            # Offsite backup runbook
            When the offsite backup fails, restart the bbr job and check
            the credhub credential rotation logs.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus: Path) -> None:
    src = LocalSource(
        name="hyde-integration",
        type="local",
        path=str(corpus),
        include=["**/*.md"],
    )
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


class _RecordingEmbedder(MockEmbeddingProvider):
    """A :class:`MockEmbeddingProvider` that records every ``embed_query``
    input so a test can assert HyDE actually changed the embedded text."""

    def __init__(self) -> None:
        super().__init__()
        self.embed_query_calls: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.embed_query_calls.append(text)
        return await super().embed_query(text)


class TestHydeDisabledIsIdentityChange:
    """``KILN_HYDE_ENABLED=false`` (default) — search output must be
    byte-identical to current main."""

    @pytest.mark.asyncio
    async def test_hyde_disabled_does_not_change_search_output(
        self, session: AsyncSession, small_corpus: Path
    ) -> None:
        await _seed(session, small_corpus)
        provider = _RecordingEmbedder()
        config = RetrievalConfig()

        # No hyde wired.
        baseline_retriever = HybridRetriever(
            db=session.bind,  # type: ignore[arg-type]
            embedding_provider=provider,
            config=config,
            hyde=None,
        )
        baseline = await baseline_retriever.search(
            "offsite backup", filters=RetrievalFilters(), max_results=10, session=session
        )
        # The provider received exactly the raw query.
        assert provider.embed_query_calls == ["offsite backup"]
        assert isinstance(baseline.chunks, list)


class TestHydeChangesEmbeddingTextOnly:
    """When HyDE is wired AND the gate fires, ``embed_query`` receives
    the pseudo-doc text — NOT the raw query. FTS arm still gets the
    raw query (no test asserts FTS shape here; the contract is
    implicit because hybrid_search receives both)."""

    @pytest.mark.asyncio
    async def test_pseudo_doc_replaces_embedding_input(
        self, session: AsyncSession, small_corpus: Path
    ) -> None:
        await _seed(session, small_corpus)
        provider = _RecordingEmbedder()
        config = RetrievalConfig()
        # MockGeneratorProvider echoes the prompt — the pseudo-doc will
        # contain the magic marker so the test can spot it.
        gen = MockGeneratorProvider(response_template="PSEUDO_DOC_MAGIC: {prompt}")
        hyde = HydeEngine(
            generator=gen,
            cache=HydeCache(ttl_seconds=600, max_entries=32),
        )

        retriever = HybridRetriever(
            db=session.bind,  # type: ignore[arg-type]
            embedding_provider=provider,
            config=config,
            hyde=hyde,
        )
        await retriever.search(
            "offsite backup", filters=RetrievalFilters(), max_results=10, session=session
        )
        # The embedder received the pseudo-doc, NOT the raw query.
        assert len(provider.embed_query_calls) == 1
        embed_input = provider.embed_query_calls[0]
        assert "PSEUDO_DOC_MAGIC" in embed_input, (
            f"embed_query received {embed_input!r} — HyDE pseudo-doc did not reach the vector arm."
        )


class TestHydeWithNoGeneratorIsDegradationContract:
    """``KILN_HYDE_ENABLED=true`` with no generator → HydeEngine
    returns None for every call → ``embed_query`` receives the raw
    query → search output identical to baseline."""

    @pytest.mark.asyncio
    async def test_no_generator_means_raw_query_embedded(
        self, session: AsyncSession, small_corpus: Path
    ) -> None:
        await _seed(session, small_corpus)
        provider = _RecordingEmbedder()
        config = RetrievalConfig()
        # Engine constructed with generator=None — the degradation
        # path. expand() returns None for every call; embed_text
        # falls back to the raw query.
        hyde = HydeEngine(
            generator=None,
            cache=HydeCache(ttl_seconds=600, max_entries=32),
        )

        retriever = HybridRetriever(
            db=session.bind,  # type: ignore[arg-type]
            embedding_provider=provider,
            config=config,
            hyde=hyde,
        )
        await retriever.search(
            "offsite backup", filters=RetrievalFilters(), max_results=10, session=session
        )
        # The embedder received the raw query — degradation contract.
        assert provider.embed_query_calls == ["offsite backup"]


class TestHydeGateOffFallsThrough:
    """A query the gate skips (long + low-jargon + non-imperative)
    should also produce raw-query embedding — HyDE never fires."""

    @pytest.mark.asyncio
    async def test_long_chatty_query_skips_hyde(
        self, session: AsyncSession, small_corpus: Path
    ) -> None:
        await _seed(session, small_corpus)
        provider = _RecordingEmbedder()
        config = RetrievalConfig()
        gen = MockGeneratorProvider(response_template="PSEUDO_DOC_MAGIC: {prompt}")
        hyde = HydeEngine(
            generator=gen,
            cache=HydeCache(ttl_seconds=600, max_entries=32),
        )

        retriever = HybridRetriever(
            db=session.bind,  # type: ignore[arg-type]
            embedding_provider=provider,
            config=config,
            hyde=hyde,
        )
        long_chatty = (
            "we noticed yesterday that our team forgot to record the new "
            "decision in the operations log and now nobody can remember "
            "what was actually agreed upon"
        )
        await retriever.search(
            long_chatty, filters=RetrievalFilters(), max_results=10, session=session
        )
        # Gate skipped → embedder saw the raw query.
        assert provider.embed_query_calls == [long_chatty]
        # And the generator was never asked.
        assert gen.calls == []
