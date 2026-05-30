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

from cf_knowledge_kiln.api.tracing import get_tracer
from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.db.repositories._hybrid import SearchRow
from cf_knowledge_kiln.db.repositories.documents import ChunksRepository
from cf_knowledge_kiln.retrieval._engine_helpers import (
    collect_warnings as _collect_warnings,
)
from cf_knowledge_kiln.retrieval._engine_helpers import (
    conflict_warnings as _conflict_warnings,
)
from cf_knowledge_kiln.retrieval._engine_helpers import (
    document_refs_from_rows as _document_refs_from_rows,
)
from cf_knowledge_kiln.retrieval._engine_helpers import (
    embedding_text_for_vector_arm as _embedding_text_for_vector_arm,
)
from cf_knowledge_kiln.retrieval._engine_helpers import (
    query_normalized_warning as _query_normalized_warning,
)
from cf_knowledge_kiln.retrieval._engine_helpers import (
    require_nonempty as _require_nonempty,
)
from cf_knowledge_kiln.retrieval._engine_helpers import (
    row_to_ranked_chunk as _row_to_ranked_chunk,
)
from cf_knowledge_kiln.retrieval.config import RetrievalConfig
from cf_knowledge_kiln.retrieval.query_normalization import normalize_query
from cf_knowledge_kiln.retrieval.ranking import (
    RankedChunk,
    apply_boosts,
    detect_conflicts,
)
from cf_knowledge_kiln.retrieval.types import (
    ContextPackResponse,
    RetrievalFilters,
    Warning,
)

# OTel Phase 2 — module-scoped tracer for retrieval-phase spans. The
# tracer is a no-op when the ``[otel]`` extra isn't installed AND a
# no-op when the extra IS installed but no TracerProvider was wired
# at startup (the default for ``KILN_OTEL_EXPORTER_OTLP_ENDPOINT``
# unset). Span emission only costs anything when an operator turned
# tracing on. Attribute vocabulary: see docs/observability.md.
_TRACER = get_tracer(__name__)

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

    Retrieval only needs the QUERY side (``embed_query`` plus the
    metadata attrs). ``embed_documents`` lives on the ingestion-side
    Protocol; we don't need it here. ``aclose()`` is intentionally
    omitted — the retrieval engine borrows a provider, it does not own
    its lifecycle. Whoever constructed the provider (the API
    ``lifespan`` or the ingestion worker) is responsible for calling
    ``aclose()`` on shutdown.
    """

    provider: str
    model: str
    dimensions: int

    async def embed_query(self, text: str) -> list[float]: ...


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
        hyde: object | None = None,
    ) -> None:
        self._db = db
        self._provider = embedding_provider
        self._config = config
        self._ef_search = ef_search
        self._prompt_injection_phrases = prompt_injection_phrases or []
        # #333: HyDE engine, optional. None = standard retrieval. Typed
        # ``object`` to avoid the import cycle (retrieval/hyde/engine.py
        # depends on retrieval helpers); runtime contract is ``async
        # def expand(query) -> str | None``.
        self._hyde = hyde

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
        with _TRACER.start_as_current_span(
            "retrieval.search",
            attributes={
                "retrieval.consumer_type": "human",
                "retrieval.query_length": len(query),
                "retrieval.max_results": max_results,
            },
        ) as root_span:
            _require_nonempty(query)
            # #100: strip configured prompt-injection markers from the
            # query before retrieval. If everything got stripped the call
            # treats it as an empty query (400 at the API layer).
            with _TRACER.start_as_current_span("retrieval.normalize_query") as norm_span:
                cleaned, removed_phrases = normalize_query(query, self._prompt_injection_phrases)
                norm_span.set_attribute("retrieval.removed_phrases_count", len(removed_phrases))
            if removed_phrases and not cleaned:
                raise ValueError("query consists entirely of prompt-injection markers")
            effective = cleaned if removed_phrases else query
            rows = await self._fetch_candidates(effective, filters, session=session)
            with _TRACER.start_as_current_span("retrieval.apply_boosts") as boost_span:
                chunks = [_row_to_ranked_chunk(r) for r in rows]
                boosted = apply_boosts(chunks, config=self._config, today=date.today())
                boosted.sort(key=lambda c: c.score, reverse=True)
                trimmed = boosted[:max_results]
                boost_span.set_attribute("retrieval.chunks_in", len(chunks))
                boost_span.set_attribute("retrieval.chunks_kept", len(trimmed))
            with _TRACER.start_as_current_span("retrieval.collect_warnings") as warn_span:
                warnings = _collect_warnings(
                    trimmed,
                    today=date.today(),
                    stale_after_days=self._config.stale_after_days,
                    weak_evidence_threshold=self._config.weak_evidence_score_threshold,
                    relevance_floor=self._config.effective_relevance_floor,
                    max_warning_rank=self._config.max_warning_rank,
                    isolated_match_drop_threshold=self._config.isolated_match_drop_threshold,
                )
                if removed_phrases:
                    warnings.append(_query_normalized_warning(removed_phrases))
                warn_span.set_attribute("retrieval.warnings_count", len(warnings))
            trimmed_ids = {c.chunk_id for c in trimmed}
            root_span.set_attribute("retrieval.chunks_returned", len(trimmed))
            root_span.set_attribute("retrieval.warnings_count", len(warnings))
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
        embed_warnings_in_text: bool = False,
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

        with _TRACER.start_as_current_span(
            "retrieval.context_pack",
            attributes={
                "retrieval.consumer_type": "agent",
                "retrieval.query_length": len(query),
                "retrieval.max_chunks": max_chunks,
                "retrieval.max_tokens": max_tokens,
            },
        ) as root_span:
            _require_nonempty(query)
            if not task or not task.strip():
                raise ValueError("task must be a non-empty string")
            # #100: same normalization the human path does.
            with _TRACER.start_as_current_span("retrieval.normalize_query") as norm_span:
                cleaned, removed_phrases = normalize_query(query, self._prompt_injection_phrases)
                norm_span.set_attribute("retrieval.removed_phrases_count", len(removed_phrases))
            if removed_phrases and not cleaned:
                raise ValueError("query consists entirely of prompt-injection markers")
            effective = cleaned if removed_phrases else query
            rows = await self._fetch_candidates(effective, filters, session=session)
            with _TRACER.start_as_current_span("retrieval.apply_boosts") as boost_span:
                chunks = [_row_to_ranked_chunk(r) for r in rows]
                boosted = apply_boosts(chunks, config=self._config, today=date.today())
                boosted.sort(key=lambda c: c.score, reverse=True)
                trimmed = boosted[:max_chunks]
                boost_span.set_attribute("retrieval.chunks_in", len(chunks))
                boost_span.set_attribute("retrieval.chunks_kept", len(trimmed))
            trimmed_ids = {c.chunk_id for c in trimmed}
            with _TRACER.start_as_current_span("retrieval.collect_warnings") as warn_span:
                warnings = _collect_warnings(
                    trimmed,
                    today=date.today(),
                    stale_after_days=self._config.stale_after_days,
                    weak_evidence_threshold=self._config.weak_evidence_score_threshold,
                    relevance_floor=self._config.effective_relevance_floor,
                    max_warning_rank=self._config.max_warning_rank,
                    isolated_match_drop_threshold=self._config.isolated_match_drop_threshold,
                )
                warn_span.set_attribute("retrieval.warnings_count", len(warnings))
            with _TRACER.start_as_current_span("retrieval.detect_conflicts") as conf_span:
                conflicts = detect_conflicts(
                    trimmed,
                    relevance_floor=self._config.effective_relevance_floor,
                    max_warning_rank=self._config.max_warning_rank,
                )
                conf_span.set_attribute("retrieval.conflicts_count", len(conflicts))
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
            with _TRACER.start_as_current_span("retrieval.assemble_context_pack") as asm_span:
                pack = assemble_context_pack(
                    inputs,
                    task=task,
                    query=query,
                    max_chunks=max_chunks,
                    max_tokens=max_tokens,
                    weak_evidence_threshold=self._config.weak_evidence_score_threshold,
                    embed_warnings_in_text=embed_warnings_in_text,
                )
                asm_span.set_attribute(
                    "retrieval.tokens_used_estimate", pack.token_budget.used_estimate
                )
                asm_span.set_attribute(
                    "retrieval.requires_human_review", pack.requires_human_review
                )
            root_span.set_attribute("retrieval.chunks_returned", len(trimmed))
            root_span.set_attribute("retrieval.warnings_count", len(warnings))
            root_span.set_attribute("retrieval.conflicts_count", len(conflicts))
            return pack

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
            with _TRACER.start_as_current_span("retrieval.sql.fts_search") as span:
                rows = await repo.search_by_fts(query_text=query, filters=filters)
                span.set_attribute("retrieval.rows_returned", len(rows))
            return list(rows)
        with _TRACER.start_as_current_span("retrieval.hyde") as hyde_span:
            # #333: consult HyDE. None / no-expand → use raw query.
            embed_text, used_hyde = await _embedding_text_for_vector_arm(query, self._hyde)
            hyde_span.set_attribute("retrieval.hyde.gated_on", used_hyde)
        with _TRACER.start_as_current_span("retrieval.embed_query") as embed_span:
            embed_span.set_attribute("retrieval.embedding.provider", self._provider.provider)
            embed_span.set_attribute("retrieval.embedding.model", self._provider.model)
            embed_span.set_attribute("retrieval.embedding.dimensions", self._provider.dimensions)
            # #204: the provider applies the model-family query prefix
            # (e5 ``query: ``, Nomic ``search_query: ``) inside
            # embed_query. #333: pass either the raw query or the HyDE
            # pseudo-doc; FTS arm below still uses the original query.
            embedding = await self._provider.embed_query(embed_text)
        with _TRACER.start_as_current_span("retrieval.sql.hybrid_search") as sql_span:
            sql_span.set_attribute("retrieval.ef_search", self._ef_search)
            rows = await repo.hybrid_search(
                query_text=query,
                query_embedding=embedding,
                dimensions=self._provider.dimensions,
                filters=filters,
                ef_search=self._ef_search,
            )
            sql_span.set_attribute("retrieval.rows_returned", len(rows))
        return list(rows)


__all__ = [
    "EmbeddingProvider",
    "HybridRetriever",
    "SearchResult",
]
