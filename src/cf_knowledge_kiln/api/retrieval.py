"""Phase 5 routes — human search + agent context pack.

Both routes delegate to :class:`HybridRetriever`. The ranked-chunk
output of slice 2 is mapped to :class:`ResultCard` (human shape) for
``/v1/search`` and to :class:`ContextPackResponse` (agent shape) for
``/v1/agent/context-pack``. Each call is logged to the operational
tables (``rag_queries`` and ``context_packs``) so the Phase 9 eval
harness has telemetry.

Per ADR-0003: the OpenAPI contract is canonical. Pydantic shapes here
match ``openapi/openapi.yaml`` 1:1 and the drift test in
``tests/unit/test_openapi_drift.py`` enforces it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.dependencies import get_hybrid_retriever, get_session
from cf_knowledge_kiln.db.repositories import ContextPacksRepository, QueriesRepository
from cf_knowledge_kiln.retrieval import (
    ContextPackRequest,
    ContextPackResponse,
    HybridRetriever,
    RankedChunk,
    ResultCard,
    SearchRequest,
    SearchResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


@router.post(
    "/v1/search",
    operation_id="humanSearch",
    summary="Human search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
    responses={status.HTTP_200_OK: {"description": "Search results."}},
)
async def human_search(
    body: SearchRequest,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SearchResponse:
    """Run a hybrid retrieval query and return ranked result cards.

    Persists the query into ``rag_queries`` (consumer_type='human') for
    the Phase 9 eval harness. Issue #74: retrieval + telemetry share
    one DB session per request.
    """
    filters = body.filters or _empty_filters()
    try:
        result = await retriever.search(
            body.query, filters=filters, max_results=body.max_results, session=session
        )
    except ValueError as exc:
        # Defensive: Pydantic min_length=1 catches empty already.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    response = SearchResponse(
        query=body.query,
        results=[
            _chunk_to_result_card(
                c,
                result.document_refs.get(c.document_id),
                result.chunk_text.get(c.chunk_id, ""),
            )
            for c in result.chunks
        ],
        warnings=result.warnings or None,
    )
    await _log_rag_query(
        session,
        query=body.query,
        consumer_type="human",
        filters=filters.model_dump(exclude_none=True),
        chunk_ids=[c.chunk_id for c in result.chunks],
    )
    return response


@router.post(
    "/v1/agent/context-pack",
    operation_id="agentContextPack",
    summary="Build an agent context pack",
    response_model=ContextPackResponse,
    response_model_exclude_none=True,
    tags=["agent"],
    responses={status.HTTP_200_OK: {"description": "Context pack."}},
)
async def agent_context_pack(
    body: ContextPackRequest,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ContextPackResponse:
    """Build a bounded, cited context pack for an agent consumer.

    Persists into ``context_packs`` so an operator can audit what the
    agent saw (and so the eval harness can replay). Issue #74:
    retrieval + telemetry share one DB session per request.
    """
    filters = body.filters or _empty_filters()
    try:
        pack = await retriever.context_pack(
            body.query,
            task=body.task,
            filters=filters,
            max_chunks=body.max_chunks,
            max_tokens=body.max_tokens,
            session=session,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _log_context_pack(session, body=body, pack=pack)
    return pack


# ─── helpers ────────────────────────────────────────────────────────


def _empty_filters() -> Any:
    # Lazy-imported so the type isn't referenced at module scope —
    # mirrors the same cycle-avoidance pattern the engine uses.
    from cf_knowledge_kiln.retrieval import RetrievalFilters

    return RetrievalFilters()


def _chunk_to_result_card(chunk: RankedChunk, ref: object | None, content: str) -> ResultCard:
    """Map a :class:`RankedChunk` + :class:`DocumentRef` + content to a card.

    ``content`` is the chunk's raw text — supplied via
    ``SearchResult.chunk_text`` — so we can derive a real excerpt
    instead of falling back to the (empty) chunk metadata blob. A
    missing ``ref`` means the document was deleted between retrieval
    and serialization; we degrade rather than 500 the whole response.
    """
    title = getattr(ref, "title", None) or "(unknown)"
    return ResultCard(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        title=title,
        excerpt=content[:500],
        heading_path=list(chunk.heading_path) or None,
        repo=getattr(ref, "repo", None),
        path=getattr(ref, "path", None),
        commit_sha=getattr(ref, "commit_sha", None),
        owner=getattr(ref, "owner", None),
        status=chunk.status,  # type: ignore[arg-type]
        last_reviewed=chunk.last_reviewed,
        score=chunk.score,
    )


async def _log_rag_query(
    session: AsyncSession,
    *,
    query: str,
    consumer_type: str,
    filters: dict[str, Any],
    chunk_ids: list[Any],
) -> None:
    """Append a row to ``rag_queries``. Failures are logged, NOT raised.

    Writes inside the caller's session — issue #74 — so retrieval +
    telemetry commit (or roll back) atomically.

    A transient DB error during telemetry persistence must not turn
    a successful retrieval into a 500 for the caller. We catch and
    rollback the savepoint; the outer transaction stays alive so the
    handler can still return its 200 response.
    """
    try:
        async with session.begin_nested():
            await QueriesRepository(session).create(
                query=query,
                consumer_type=consumer_type,
                filters=filters,
                retrieved_chunk_ids=chunk_ids,
            )
    except Exception:
        logger.exception("rag_queries telemetry write failed (non-fatal)")


async def _log_context_pack(
    session: AsyncSession, *, body: ContextPackRequest, pack: ContextPackResponse
) -> None:
    """Append a row to ``context_packs``. Failures are logged, NOT raised."""
    try:
        async with session.begin_nested():
            await ContextPacksRepository(session).create(
                query=body.query,
                task=body.task,
                token_budget=pack.token_budget.requested,
                filters=(body.filters.model_dump(exclude_none=True) if body.filters else {}),
                evidence_chunk_ids=[e.chunk_id for e in pack.evidence],
                token_estimate=pack.token_budget.used_estimate,
                confidence=pack.confidence,
                # mode='json' serializes UUIDs/dates as strings so they
                # round-trip through the JSONB column cleanly.
                warnings=[w.model_dump(mode="json") for w in pack.warnings],
                requires_human_review=pack.requires_human_review,
            )
    except Exception:
        logger.exception("context_packs telemetry write failed (non-fatal)")


__all__ = ["router"]
