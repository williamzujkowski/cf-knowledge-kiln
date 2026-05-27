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

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.auth import username_for
from cf_knowledge_kiln.api.dependencies import (
    get_hybrid_retriever,
    get_search_limiter,
    get_session,
    get_trust_xff,
)
from cf_knowledge_kiln.api.error_handlers import raise_with_code
from cf_knowledge_kiln.api.idempotency import (
    REPLAY_HEADER,
    Outcome,
    check_or_replay,
    should_cache,
)
from cf_knowledge_kiln.api.idempotency import (
    store as _store_idempotent,
)
from cf_knowledge_kiln.api.rate_limit import (
    TokenBucketLimiter,
    raise_429_if_limited,
)
from cf_knowledge_kiln.api.request_id import request_id_for
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
    responses={
        status.HTTP_200_OK: {"description": "Search results."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
    },
)
async def human_search(
    request: Request,
    body: SearchRequest,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_search_limiter)],
    trust_xff: Annotated[bool, Depends(get_trust_xff)],
) -> SearchResponse | Response:
    """Run a hybrid retrieval query and return ranked result cards.

    Persists the query into ``rag_queries`` (consumer_type='human') for
    the Phase 9 eval harness. Issue #74: retrieval + telemetry share
    one DB session per request. Issue #79: per-IP rate limit.
    """
    raise_429_if_limited(limiter, request, trust_xff=trust_xff)
    # #309: idempotency dispatch. body.model_dump() is the
    # canonical-hash input — Pydantic already validated, so
    # serialization is safe + deterministic.
    idem = await check_or_replay(
        session=session,
        request=request,
        route="/v1/search",
        body=body.model_dump(mode="json", exclude_none=True),
    )
    if idem.outcome is Outcome.HIT:
        return JSONResponse(
            content=idem.cached_body,
            status_code=idem.cached_status or 200,
            headers={REPLAY_HEADER: "true"},
        )
    if idem.outcome is Outcome.CONFLICT:
        raise_with_code(
            status_code=422,
            error_code="idempotency_conflict",
            message="Idempotency-Key reuse with different body.",
        )
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
        request_id=request_id_for(request),
        requester=username_for(request),
    )
    # #309: cache the response for replay if the key was set.
    if idem.key is not None and idem.request_hash is not None and should_cache(200):
        await _store_idempotent(
            session=session,
            key=idem.key,
            route="/v1/search",
            request_hash=idem.request_hash,
            resource_id=None,
            response_body=response.model_dump(mode="json", exclude_none=True),
            response_status=200,
        )
    return response


@router.post(
    "/v1/agent/context-pack",
    operation_id="agentContextPack",
    summary="Build an agent context pack",
    response_model=ContextPackResponse,
    response_model_exclude_none=True,
    tags=["agent"],
    responses={
        status.HTTP_200_OK: {"description": "Context pack."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
    },
)
async def agent_context_pack(
    request: Request,
    body: ContextPackRequest,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_search_limiter)],
    trust_xff: Annotated[bool, Depends(get_trust_xff)],
) -> ContextPackResponse | Response:
    """Build a bounded, cited context pack for an agent consumer.

    Persists into ``context_packs`` so an operator can audit what the
    agent saw (and so the eval harness can replay). Issue #74:
    retrieval + telemetry share one DB session per request. Issue #79:
    per-IP rate limit (same bucket as /v1/search; both are DB-heavy).
    """
    raise_429_if_limited(limiter, request, trust_xff=trust_xff)
    # #309: idempotency dispatch.
    idem = await check_or_replay(
        session=session,
        request=request,
        route="/v1/agent/context-pack",
        body=body.model_dump(mode="json", exclude_none=True),
    )
    if idem.outcome is Outcome.HIT:
        return JSONResponse(
            content=idem.cached_body,
            status_code=idem.cached_status or 200,
            headers={REPLAY_HEADER: "true"},
        )
    if idem.outcome is Outcome.CONFLICT:
        raise_with_code(
            status_code=422,
            error_code="idempotency_conflict",
            message="Idempotency-Key reuse with different body.",
        )
    filters = body.filters or _empty_filters()
    try:
        pack = await retriever.context_pack(
            body.query,
            task=body.task,
            filters=filters,
            max_chunks=body.max_chunks,
            max_tokens=body.max_tokens,
            embed_warnings_in_text=body.embed_warnings_in_text,
            session=session,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _log_context_pack(
        session,
        body=body,
        pack=pack,
        request_id=request_id_for(request),
        requester=username_for(request),
    )
    # #309: cache the response for replay if the key was set.
    if idem.key is not None and idem.request_hash is not None and should_cache(200):
        await _store_idempotent(
            session=session,
            key=idem.key,
            route="/v1/agent/context-pack",
            request_hash=idem.request_hash,
            resource_id=str(pack.context_pack_id),
            response_body=pack.model_dump(mode="json", exclude_none=True),
            response_status=200,
        )
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
        source_url=getattr(ref, "source_url", None),
        commit_sha=getattr(ref, "commit_sha", None),
        owner=getattr(ref, "owner", None),
        status=chunk.status,
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
    request_id: str | None = None,
    requester: str | None = None,
) -> None:
    """Append a row to ``rag_queries``. Failures are logged, NOT raised.

    Writes inside the caller's session — issue #74 — so retrieval +
    telemetry commit (or roll back) atomically.

    A transient DB error during telemetry persistence must not turn
    a successful retrieval into a 500 for the caller. We catch and
    rollback the savepoint; the outer transaction stays alive so the
    handler can still return its 200 response.

    ``request_id`` (#260): the X-Request-ID correlation key, threaded
    from the handler via :func:`request_id_for`. Optional — bare test
    harnesses that hit the function directly without the middleware
    installed still work; the column is nullable.
    """
    try:
        async with session.begin_nested():
            await QueriesRepository(session).create(
                query=query,
                consumer_type=consumer_type,
                filters=filters,
                retrieved_chunk_ids=chunk_ids,
                request_id=request_id,
                requester=requester,
            )
    except Exception:
        logger.exception("rag_queries telemetry write failed (non-fatal)")


async def _log_context_pack(
    session: AsyncSession,
    *,
    body: ContextPackRequest,
    pack: ContextPackResponse,
    request_id: str | None = None,
    requester: str | None = None,
) -> None:
    """Append a row to ``context_packs``. Failures are logged, NOT raised.

    ``request_id`` (#260): see :func:`_log_rag_query`. Persisted
    alongside the wire-visible ``context_pack_id`` (#256) so an audit
    of a user complaint can match both the request and the response
    to a single row.
    """
    try:
        async with session.begin_nested():
            await ContextPacksRepository(session).create(
                # #256: persist the response-visible UUID as the row PK
                # so an operator looking up a complaint by
                # ``context_pack_id`` finds the audit row.
                id=pack.context_pack_id,
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
                request_id=request_id,
                requester=requester,
            )
    except Exception:
        logger.exception("context_packs telemetry write failed (non-fatal)")


__all__ = ["router"]
