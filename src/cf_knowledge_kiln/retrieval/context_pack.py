"""#402 — orchestration logic for :meth:`HybridRetriever.context_pack`.

Extracted from ``engine.py`` to keep that module under the
AGENTS 400-line soft cap. The function here owns the full
context-pack pipeline:

* normalize the query (and surface the ``QueryNormalizedWarning``
  when phrases were stripped)
* fetch candidate chunks via the caller-supplied fetcher
* apply boosts + trim to ``max_chunks``
* collect warnings + conflicts
* assemble the agent-shaped :class:`ContextPackResponse` via
  :func:`assemble_context_pack`

The method on :class:`HybridRetriever` is now a thin wrapper that
hands its dependencies down explicitly. Tests reach
:func:`build_context_pack` directly to exercise the orchestration
without spinning up a full retriever + DB.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.tracing import get_tracer
from cf_knowledge_kiln.db.repositories._hybrid import SearchRow
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
from cf_knowledge_kiln.retrieval.ranking import apply_boosts, detect_conflicts
from cf_knowledge_kiln.retrieval.types import (
    ContextPackResponse,
    RetrievalFilters,
)

_TRACER = get_tracer(__name__)

_FetcherSig = Callable[
    [str, RetrievalFilters, AsyncSession | None],
    Awaitable[list[SearchRow]],
]


async def build_context_pack(
    *,
    fetcher: _FetcherSig,
    prompt_injection_phrases: list[str],
    config: RetrievalConfig,
    query: str,
    task: str,
    filters: RetrievalFilters,
    max_chunks: int = 8,
    max_tokens: int = 3000,
    embed_warnings_in_text: bool = False,
    session: AsyncSession | None = None,
) -> ContextPackResponse:
    """Build a bounded, cited :class:`ContextPackResponse` for an agent.

    Same retrieval pipeline as the human ``search()`` path plus:

    * :func:`detect_conflicts` — syntactic same-heading conflict
      across distinct active documents
    * agent serialization in :func:`assemble_context_pack` — token
      budgeting + the standard untrusted-content notice + canonical
      ``requires_human_review`` decision

    ``fetcher`` is a callable matching :meth:`HybridRetriever
    ._fetch_candidates` so the orchestration can be exercised with a
    pure in-memory fake for tests; the caller (the retriever method)
    closes over its own ``self`` state and passes the bound method.
    """
    # Lazy import — assemble_context_pack pulls in the agent
    # serialization tree which is heavier than this module wants to
    # eagerly load. Mirrors the prior in-method import on engine.py.
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
            cleaned, removed_phrases = normalize_query(query, prompt_injection_phrases)
            norm_span.set_attribute("retrieval.removed_phrases_count", len(removed_phrases))
        if removed_phrases and not cleaned:
            raise ValueError("query consists entirely of prompt-injection markers")
        effective = cleaned if removed_phrases else query
        rows = await fetcher(effective, filters, session)
        with _TRACER.start_as_current_span("retrieval.apply_boosts") as boost_span:
            chunks = [_row_to_ranked_chunk(r) for r in rows]
            boosted = apply_boosts(chunks, config=config, today=date.today())
            boosted.sort(key=lambda c: c.score, reverse=True)
            trimmed = boosted[:max_chunks]
            boost_span.set_attribute("retrieval.chunks_in", len(chunks))
            boost_span.set_attribute("retrieval.chunks_kept", len(trimmed))
        trimmed_ids = {c.chunk_id for c in trimmed}
        with _TRACER.start_as_current_span("retrieval.collect_warnings") as warn_span:
            warnings = _collect_warnings(
                trimmed,
                today=date.today(),
                stale_after_days=config.stale_after_days,
                weak_evidence_threshold=config.weak_evidence_score_threshold,
                relevance_floor=config.effective_relevance_floor,
                max_warning_rank=config.max_warning_rank,
                isolated_match_drop_threshold=config.isolated_match_drop_threshold,
            )
            warn_span.set_attribute("retrieval.warnings_count", len(warnings))
        with _TRACER.start_as_current_span("retrieval.detect_conflicts") as conf_span:
            conflicts = detect_conflicts(
                trimmed,
                relevance_floor=config.effective_relevance_floor,
                max_warning_rank=config.max_warning_rank,
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
                weak_evidence_threshold=config.weak_evidence_score_threshold,
                embed_warnings_in_text=embed_warnings_in_text,
            )
            asm_span.set_attribute(
                "retrieval.tokens_used_estimate", pack.token_budget.used_estimate
            )
            asm_span.set_attribute("retrieval.requires_human_review", pack.requires_human_review)
        root_span.set_attribute("retrieval.chunks_returned", len(trimmed))
        root_span.set_attribute("retrieval.warnings_count", len(warnings))
        root_span.set_attribute("retrieval.conflicts_count", len(conflicts))
        return pack


__all__ = ["build_context_pack"]
