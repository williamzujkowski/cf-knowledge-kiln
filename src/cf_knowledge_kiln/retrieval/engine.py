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

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.db.repositories._hybrid import SearchRow
from cf_knowledge_kiln.db.repositories.documents import ChunksRepository
from cf_knowledge_kiln.retrieval.config import RetrievalConfig
from cf_knowledge_kiln.retrieval.query_normalization import normalize_query
from cf_knowledge_kiln.retrieval.ranking import (
    RankedChunk,
    apply_boosts,
    deprecated_warnings,
    detect_conflicts,
    prompt_injection_warnings,
    sensitive_content_warnings,
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
    ``document_refs`` maps document_id → a minimal record with the
    document-level fields (title, repo, path, …) that callers need to
    render result cards — :class:`RankedChunk` itself is chunk-level
    only and doesn't carry document metadata. The dict values are
    :class:`cf_knowledge_kiln.agent.serializers.DocumentRef` but typed
    as ``Any`` here to avoid a top-level cycle (the engine module
    can't import from agent.serializers without triggering it).
    ``chunk_text`` maps chunk_id → its raw content; the API layer uses
    this to derive an excerpt without a second DB round-trip.
    """

    chunks: list[RankedChunk]
    warnings: list[Warning] = field(default_factory=list)
    document_refs: dict[UUID, Any] = field(default_factory=dict)
    chunk_text: dict[UUID, str] = field(default_factory=dict)


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
        prompt_injection_phrases: list[str] | None = None,
    ) -> None:
        self._db = db
        self._provider = embedding_provider
        self._config = config
        self._ef_search = ef_search
        # #100: phrases used by normalize_query() to strip operator
        # markers from the inbound query. None / empty list disables
        # normalization — the retrieval path then behaves exactly as
        # it did before the feature landed.
        self._prompt_injection_phrases = prompt_injection_phrases or []

    async def search(
        self,
        query: str,
        *,
        filters: RetrievalFilters,
        max_results: int = 10,
        session: AsyncSession | None = None,
    ) -> SearchResult:
        """Run a query, return ranked chunks + warnings.

        Raises ``ValueError`` on empty/whitespace queries — the API
        layer (slice 4) translates this to a 400.

        When ``session`` is provided, the retrieval CTE runs on it (no
        new connection from the pool). Issue #74 — the API handler
        passes its own session so the same transaction can also write
        telemetry, halving the per-request connection count.
        """
        _require_nonempty(query)
        # #100: strip configured prompt-injection markers from the
        # query before retrieval. If everything got stripped the call
        # treats it as an empty query (400 at the API layer).
        cleaned, removed_phrases = normalize_query(query, self._prompt_injection_phrases)
        if removed_phrases and not cleaned:
            raise ValueError("query consists entirely of prompt-injection markers")
        effective = cleaned if removed_phrases else query
        rows = await self._fetch_candidates(effective, filters, session=session)
        chunks = [_row_to_ranked_chunk(r) for r in rows]
        boosted = apply_boosts(chunks, config=self._config, today=date.today())
        boosted.sort(key=lambda c: c.score, reverse=True)
        trimmed = boosted[:max_results]
        warnings = _collect_warnings(
            trimmed, today=date.today(), stale_after_days=self._config.stale_after_days
        )
        if removed_phrases:
            warnings.append(_query_normalized_warning(removed_phrases))
        trimmed_ids = {c.chunk_id for c in trimmed}
        return SearchResult(
            chunks=trimmed,
            warnings=warnings,
            document_refs=_document_refs_from_rows(rows),
            chunk_text={r.chunk_id: r.content for r in rows if r.chunk_id in trimmed_ids},
        )

    async def context_pack(
        self,
        query: str,
        *,
        task: str,
        filters: RetrievalFilters,
        max_chunks: int = 8,
        max_tokens: int = 3000,
        session: AsyncSession | None = None,
    ) -> ContextPackResponse:
        """Build a bounded, cited :class:`ContextPackResponse` for an agent.

        Same retrieval pipeline as :meth:`search` plus:

        * :func:`detect_conflicts` — syntactic same-heading conflict
          across distinct active documents
        * agent serialization in
          :func:`assemble_context_pack` — token budgeting + the
          standard untrusted-content notice + canonical
          ``requires_human_review`` decision

        ``session`` parameter behaves identically to :meth:`search` —
        the API handler passes its own session so retrieval +
        telemetry share one transaction (issue #74).
        """
        # Lazy import — see TYPE_CHECKING block at top for why.
        from cf_knowledge_kiln.agent.serializers import (
            SerializerInputs,
            assemble_context_pack,
        )

        _require_nonempty(query)
        if not task or not task.strip():
            raise ValueError("task must be a non-empty string")
        # #100: same normalization the human path does.
        cleaned, removed_phrases = normalize_query(query, self._prompt_injection_phrases)
        if removed_phrases and not cleaned:
            raise ValueError("query consists entirely of prompt-injection markers")
        effective = cleaned if removed_phrases else query
        rows = await self._fetch_candidates(effective, filters, session=session)
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
        if removed_phrases:
            warnings.append(_query_normalized_warning(removed_phrases))
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

    async def _fetch_candidates(
        self,
        query: str,
        filters: RetrievalFilters,
        *,
        session: AsyncSession | None,
    ) -> list[SearchRow]:
        """Fan out to hybrid CTE or FTS-only fallback, depending on provider.

        Uses the caller's ``session`` when provided (the API hot path
        — issue #74). Otherwise opens a fresh one + transaction. The
        repo method issues ``SET LOCAL hnsw.ef_search`` inside the
        transaction so the setting doesn't leak past it.
        """
        async with self._session_in_txn(session) as txn_session:
            return await self._run_query(txn_session, query, filters)

    @asynccontextmanager
    async def _session_in_txn(
        self, session: AsyncSession | None
    ) -> Any:  # AsyncIterator[AsyncSession]
        """Yield a session inside a transaction.

        * Caller-supplied session: caller MUST have already started an
          active transaction (e.g., the API handler via
          ``Depends(get_session)``). Passing an un-transactioned
          session is undefined behavior — the per-query
          ``SET LOCAL hnsw.ef_search`` won't take effect outside a
          transaction and the recall target will silently regress.
        * No session: open one via the pool and start a transaction
          here.
        """
        if session is not None:
            yield session
            return
        async with self._db.session() as fresh, fresh.begin():
            yield fresh

    async def _run_query(
        self, session: AsyncSession, query: str, filters: RetrievalFilters
    ) -> list[SearchRow]:
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
        has_sensitive_content=row.has_sensitive_content,
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
    warnings.extend(sensitive_content_warnings(chunks))
    warnings.extend(weak_evidence_warning(chunks))
    return warnings


def _conflict_warnings(conflicts: list[Conflict]) -> list[Warning]:
    """One ``conflicting_sources`` warning per detected conflict.

    Conflicts are dual-surfaced: as structured :class:`Conflict`
    entries on the response AND as warning entries. The structured
    list is canonical for the ``requires_human_review`` decision
    (see :func:`ranking.requires_human_review` — it inspects the
    ``conflicts`` argument, not the warnings argument); the warning
    is purely for agents that only consume the warnings channel and
    would otherwise miss conflict surfacing.
    """
    return [
        Warning(
            type="conflicting_sources",
            message=f"{len(c.source_ids)} active sources address {c.topic!r}.",
        )
        for c in conflicts
    ]


def _query_normalized_warning(removed_phrases: list[str]) -> Warning:
    """One ``query_normalized`` warning when the caller's query was sanitized (#100).

    Lists the phrase sources that matched so an operator auditing the
    response can spot a query attempting to exfiltrate prompt-
    injection content from the corpus. The list is informational —
    the cleaned query has already gone through retrieval.
    """
    sample = ", ".join(repr(p) for p in removed_phrases[:3])
    suffix = f" (and {len(removed_phrases) - 3} more)" if len(removed_phrases) > 3 else ""
    return Warning(
        type="query_normalized",
        message=(
            f"Query contained prompt-injection markers; stripped before retrieval: "
            f"{sample}{suffix}."
        ),
    )


def _document_refs_from_rows(rows: list[SearchRow]) -> dict[UUID, Any]:
    """Build ``{document_id: DocumentRef}`` from search rows.

    SearchRow carries the document-level fields the EvidenceChunk
    shape needs; collapse to one ref per document_id (later rows
    don't overwrite — same document, same metadata). ``DocumentRef``
    is lazy-imported here to avoid the retrieval ↔ agent cycle.

    ``source_url`` flows from ``documents.source_url`` through the
    CTE projection (#24); ingestion populates it from frontmatter
    ``source_url:`` for now. ``None`` is fine — the UI falls back to
    rendering the plain ``repo/path`` string.
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
            source_url=row.source_url,
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
