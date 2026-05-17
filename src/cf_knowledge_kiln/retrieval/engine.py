"""Phase 5 hybrid retrieval engine.

:class:`HybridRetriever` glues the pure-logic primitives in
:mod:`cf_knowledge_kiln.retrieval.ranking` together with the
DB-touching CTE in :class:`ChunksRepository.hybrid_search`. One method
for slice 2 — :meth:`HybridRetriever.search` — embeds the query, fans
out vector + FTS arms, fuses with RRF, applies status/freshness
boosts, sorts, trims to ``max_results``, and emits the standard
warning set. No HTTP, no agent-shape serialization (those land in
slices 3 and 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.db.repositories._hybrid import SearchRow
from cf_knowledge_kiln.db.repositories.documents import ChunksRepository
from cf_knowledge_kiln.retrieval.config import RetrievalConfig
from cf_knowledge_kiln.retrieval.ranking import (
    RankedChunk,
    apply_boosts,
    deprecated_warnings,
    prompt_injection_warnings,
    stale_warnings,
    weak_evidence_warning,
)
from cf_knowledge_kiln.retrieval.types import RetrievalFilters, Warning


class EmbeddingProvider(Protocol):
    """Slice of :mod:`cf_knowledge_kiln.ingestion.embedding.EmbeddingProvider`.

    Locally redeclared so this module doesn't import the ingestion
    layer (architecturally, retrieval should not depend on ingestion).
    Any object matching this Protocol — including ``MockEmbeddingProvider``
    — works here.
    """

    provider: str
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class SearchResult:
    """What :meth:`HybridRetriever.search` returns.

    ``chunks`` are post-boost, sorted best-first, trimmed to
    ``max_results``. ``warnings`` are de-duplicated per document_id
    where applicable (see ranking warning emitters for the exact rules).
    """

    chunks: list[RankedChunk]
    warnings: list[Warning] = field(default_factory=list)


class HybridRetriever:
    """Orchestrate hybrid pgvector + FTS retrieval per ADR-0009.

    Wires a :class:`Database` (for sessions), an optional
    :class:`EmbeddingProvider` (None = FTS-only fallback), and a
    :class:`RetrievalConfig` (status weights + stale window).

    Slice 2 exposes one public method: :meth:`search`. Slice 3 will
    add :meth:`context_pack` for the agent surface.
    """

    def __init__(
        self,
        db: Database,
        embedding_provider: EmbeddingProvider | None,
        config: RetrievalConfig,
        *,
        ef_search: int = 200,
    ) -> None:
        self._db = db
        self._provider = embedding_provider
        self._config = config
        self._ef_search = ef_search

    async def search(
        self,
        query: str,
        *,
        filters: RetrievalFilters,
        max_results: int = 10,
    ) -> SearchResult:
        """Run a query, return ranked chunks + warnings.

        Raises ``ValueError`` on empty/whitespace queries — the API
        layer (slice 4) translates this to a 400.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string")
        rows = await self._fetch_candidates(query, filters)
        chunks = [_row_to_ranked_chunk(r) for r in rows]
        boosted = apply_boosts(chunks, config=self._config, today=date.today())
        boosted.sort(key=lambda c: c.score, reverse=True)
        trimmed = boosted[:max_results]
        warnings = _collect_warnings(
            trimmed, today=date.today(), stale_after_days=self._config.stale_after_days
        )
        return SearchResult(chunks=trimmed, warnings=warnings)

    async def _fetch_candidates(self, query: str, filters: RetrievalFilters) -> list[SearchRow]:
        """Fan out to hybrid CTE or FTS-only fallback, depending on provider.

        Opens a transaction so ``SET LOCAL hnsw.ef_search`` is scoped
        correctly. The repo method handles the SET internally.
        """
        async with self._db.session() as session, session.begin():
            repo = ChunksRepository(session)
            if self._provider is None:
                rows = await repo.search_by_fts(query_text=query, filters=filters)
                return list(rows)
            embedding = (await self._provider.embed([query]))[0]
            rows = await repo.hybrid_search(
                query_text=query,
                query_embedding=embedding,
                dimensions=self._provider.dimensions,
                filters=filters,
                ef_search=self._ef_search,
            )
            return list(rows)


def _row_to_ranked_chunk(row: SearchRow) -> RankedChunk:
    return RankedChunk(
        chunk_id=row.chunk_id,
        document_id=row.document_id,
        score=row.score,
        status=row.status,
        heading_path=row.heading_path,
        authority=row.authority,
        last_reviewed=row.last_reviewed,
        has_prompt_injection=row.has_prompt_injection,
        chunk_metadata=row.chunk_metadata,
    )


def _collect_warnings(
    chunks: list[RankedChunk], *, today: date, stale_after_days: int | None
) -> list[Warning]:
    """Concatenate the standard slice-2 warning set."""
    warnings: list[Warning] = []
    warnings.extend(stale_warnings(chunks, today=today, stale_after_days=stale_after_days))
    warnings.extend(deprecated_warnings(chunks))
    warnings.extend(prompt_injection_warnings(chunks))
    warnings.extend(weak_evidence_warning(chunks))
    return warnings


__all__ = [
    "EmbeddingProvider",
    "HybridRetriever",
    "SearchResult",
]
