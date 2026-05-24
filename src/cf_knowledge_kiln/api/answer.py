"""POST /v1/answer route — Phase C of /v1/answer (#192).

Thin wrapper around :func:`cf_knowledge_kiln.agent.answer.synthesize_answer`.
The route's only job is to:

* dependency-inject the per-request retriever, the shared generator,
  the per-request DB session, and the rate-limit decision;
* validate the inbound JSON via :class:`AnswerRequest`;
* call ``synthesize_answer``;
* persist a telemetry row (non-fatal — a write failure here doesn't
  cascade to a 500, mirroring the context-pack pattern);
* return :class:`AnswerResponse`.

The generator dependency returns 503 when no generator is wired —
the MVP default. Operators bring up /v1/answer by enabling the
generator block in ``config/models.yaml`` and setting the
``KILN_GENERATOR_*`` env vars.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.agent.answer import synthesize_answer
from cf_knowledge_kiln.api.dependencies import (
    get_generator_provider,
    get_hybrid_retriever,
    get_search_limiter,
    get_session,
    get_trust_xff,
)
from cf_knowledge_kiln.api.rate_limit import (
    TokenBucketLimiter,
    raise_429_if_limited,
)
from cf_knowledge_kiln.db.repositories import QueriesRepository
from cf_knowledge_kiln.generation import GeneratorProvider
from cf_knowledge_kiln.retrieval import HybridRetriever
from cf_knowledge_kiln.retrieval.types import AnswerRequest, AnswerResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/v1/answer",
    operation_id="answer",
    summary="Synthesize a cited answer from retrieved evidence",
    response_model=AnswerResponse,
    response_model_exclude_none=True,
    tags=["agent"],
    responses={
        status.HTTP_200_OK: {"description": "Cited answer or refusal."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "No generator configured. See config/models.yaml."
        },
    },
)
async def answer(
    request: Request,
    body: AnswerRequest,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    generator: Annotated[GeneratorProvider, Depends(get_generator_provider)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_search_limiter)],
    trust_xff: Annotated[bool, Depends(get_trust_xff)],
) -> AnswerResponse:
    """Run hybrid retrieval → synthesize a cited answer → return it.

    Same rate-limit bucket as /v1/search and /v1/agent/context-pack
    (#79 — these endpoints share the same DB + generator cost
    profile from the API's POV). Telemetry write failures are
    non-fatal: a successful synthesis still returns 200 even if the
    rag_queries row can't be persisted (mirrors #172).
    """
    raise_429_if_limited(limiter, request, trust_xff=trust_xff)
    try:
        response = await synthesize_answer(retriever, generator, body, session=session)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _log_answer_query(session, body=body, response=response)
    return response


async def _log_answer_query(
    session: AsyncSession, *, body: AnswerRequest, response: AnswerResponse
) -> None:
    """Append a ``rag_queries`` row tagged ``consumer_type='agent'``.

    Failures are logged, NOT raised — a transient DB hiccup during
    telemetry persistence must not turn a successful answer into a
    500 for the caller. Mirrors the existing context-pack telemetry
    pattern (#172 trap #21).

    Uses ``consumer_type='agent'`` because the existing
    ``ck_queries_consumer_type`` CHECK constraint only allows
    ``'human'`` and ``'agent'`` — /v1/answer is an agent-shaped
    endpoint, so the value is semantically correct. Distinguishing
    answer-rows from context-pack-rows in the same table is a
    follow-up: a dedicated ``rag_answers`` table (with the
    generator-side metadata + finish_reason + truncation/refusal
    flag) would earn its keep once the eval harness needs to slice
    on it. Migration deferred to keep this PR focused.
    """
    try:
        async with session.begin_nested():
            await QueriesRepository(session).create(
                query=body.query,
                consumer_type="agent",
                filters=(body.filters.model_dump(exclude_none=True) if body.filters else {}),
                retrieved_chunk_ids=[e.chunk_id for e in response.evidence],
            )
    except Exception:
        logger.exception("rag_queries telemetry write for /v1/answer failed (non-fatal)")


__all__ = ["router"]
