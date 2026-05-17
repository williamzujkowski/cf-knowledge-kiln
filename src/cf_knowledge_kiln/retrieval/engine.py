"""Phase 5 hybrid retrieval engine.

:class:`HybridRetriever` glues the pure-logic primitives in
:mod:`cf_knowledge_kiln.retrieval.ranking` together with the
DB-touching CTE in :class:`ChunksRepository.hybrid_search`.

Public methods:

* :meth:`HybridRetriever.search` (slice 2) — embed, fan out vector +
  FTS arms, fuse via RRF, apply boosts, return top-N + warnings.
* :meth:`HybridRetriever.context_pack` (slice 3) — same retrieval
  pipeline plus conflict detection, token budgeting, and
  agent-shape serialization (returns :class:`ContextPackResponse`).

Slice 4 will replace the 501 stubs in ``api/retrieval.py`` with HTTP
handlers calling these two methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.db.repositories._hybrid import SearchRow
from cf_knowledge_kiln.db.repositories.documents import ChunksRepository
from cf_knowledge_kiln.retrieval.config import RetrievalConfig
from cf_knowledge_kiln.retrieval.ranking import (
    RankedChunk,
    apply_boosts,
    deprecated_warnings,
    detect_conflicts,
    prompt_injection_warnings,
    stale_warnings,
    weak_evidence_warning,
)
from cf_knowledge_kiln.retrieval.types import (
    Conflict,
    ContextPackResponse,
    RetrievalFilters,
    Warning,
)

# NOTE: ``cf_knowledge_kiln.agent.serializers`` is intentionally NOT
# imported at module load. ``retrieval/__init__.py`` re-exports
# ``HybridRetriever``, and the serializer depends on
# ``retrieval.ranking`` — initializing the package forces a cycle.
# The lazy import inside ``context_pack`` + ``_document_refs_from_rows``
# breaks it. No TYPE_CHECKING import is needed because the engine
# module never references the serializer types at module scope.


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
        _require_nonempty(query)
        rows = await self._fetch_candidates(query, filters)
        chunks = [_row_to_ranked_chunk(r) for r in rows]
        boosted = apply_boosts(chunks, config=self._config, today=date.today())
        boosted.sort(key=lambda c: c.score, reverse=True)
        trimmed = boosted[:max_results]
        warnings = _collect_warnings(
            trimmed, today=date.today(), stale_after_days=self._config.stale_after_days
        )
        return SearchResult(chunks=trimmed, warnings=warnings)

    async def context_pack(
        self,
        query: str,
        *,
        task: str,
        filters: RetrievalFilters,
        max_chunks: int = 8,
        max_tokens: int = 3000,
    ) -> ContextPackResponse:
        """Build a bounded, cited :class:`ContextPackResponse` for an agent.

        Same retrieval pipeline as :meth:`search` plus:

        * :func:`detect_conflicts` — syntactic same-heading conflict
          across distinct active documents
        * agent serialization in
          :func:`assemble_context_pack` — token budgeting + the
          standard untrusted-content notice + canonical
          ``requires_human_review`` decision
        """
        # Lazy import — see TYPE_CHECKING block at top for why.
        from cf_knowledge_kiln.agent.serializers import (
            SerializerInputs,
            assemble_context_pack,
        )

        _require_nonempty(query)
        if not task or not task.strip():
            raise ValueError("task must be a non-empty string")
        rows = await self._fetch_candidates(query, filters)
        chunks = [_row_to_ranked_chunk(r) for r in rows]
        boosted = apply_boosts(chunks, config=self._config, today=date.today())
        boosted.sort(key=lambda c: c.score, reverse=True)
        trimmed = boosted[:max_chunks]
        trimmed_ids = {c.chunk_id for c in trimmed}
        warnings = _collect_warnings(
            trimmed, today=date.today(), stale_after_days=self._config.stale_after_days
        )
        conflicts = detect_conflicts(trimmed)
        warnings.extend(_conflict_warnings(conflicts))
        inputs = SerializerInputs(
            chunks=trimmed,
            warnings=warnings,
            conflicts=conflicts,
            chunk_text={r.chunk_id: r.content for r in rows if r.chunk_id in trimmed_ids},
            document_refs=_document_refs_from_rows(rows),
            related_sources=[],
        )
        return assemble_context_pack(
            inputs, task=task, query=query, max_chunks=max_chunks, max_tokens=max_tokens
        )

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


def _conflict_warnings(conflicts: list[Conflict]) -> list[Warning]:
    """One ``conflicting_sources`` warning per detected conflict.

    Slice 3 surfaces conflicts both as :class:`Conflict` entries
    (structured) and as warnings (so agents that only look at the
    warning channel still see them).
    """
    return [
        Warning(
            type="conflicting_sources",
            message=f"{len(c.source_ids)} active sources address {c.topic!r}.",
        )
        for c in conflicts
    ]


def _document_refs_from_rows(rows: list[SearchRow]) -> dict[UUID, Any]:
    """Build ``{document_id: DocumentRef}`` from search rows.

    SearchRow carries the document-level fields the EvidenceChunk
    shape needs; collapse to one ref per document_id (later rows
    don't overwrite — same document, same metadata). ``DocumentRef``
    is lazy-imported here to avoid the retrieval ↔ agent cycle.
    """
    from cf_knowledge_kiln.agent.serializers import DocumentRef

    refs: dict[UUID, Any] = {}
    for row in rows:
        if row.document_id in refs:
            continue
        refs[row.document_id] = DocumentRef(
            document_id=row.document_id,
            title=row.title,
            repo=row.repo,
            path=row.path,
            source_url=None,
            commit_sha=row.commit_sha,
            authority=row.authority,
            owner=row.owner,
        )
    return refs


def _require_nonempty(query: str) -> None:
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")


__all__ = [
    "EmbeddingProvider",
    "HybridRetriever",
    "SearchResult",
]
