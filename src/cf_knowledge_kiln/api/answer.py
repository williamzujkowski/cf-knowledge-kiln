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
from cf_knowledge_kiln.db.repositories import AnswersRepository
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
    """Append a ``rag_answers`` row with the full response classification (#221).

    Failures are logged, NOT raised — a transient DB hiccup during
    telemetry persistence must not turn a successful answer into a
    500 for the caller. Mirrors the existing context-pack telemetry
    pattern (#172 trap #21).

    Writes to ``rag_answers`` (not the shared ``rag_queries``) so the
    answer-side signals — refusal class, generator metadata,
    finish_reason, token counts — are captured at write time and
    don't need to be reconstructed from JSON later. The migration
    landed alongside this change.
    """
    try:
        async with session.begin_nested():
            await AnswersRepository(session).create(
                query=body.query,
                task=body.task,
                filters=(body.filters.model_dump(exclude_none=True) if body.filters else {}),
                evidence_chunk_ids=[e.chunk_id for e in response.evidence],
                answerable=response.answerable,
                requires_human_review=response.requires_human_review,
                refusal_reason=response.refusal_reason,
                confidence=response.confidence,
                generator_provider=response.generator_provider,
                generator_model=response.generator_model,
                finish_reason=response.token_budget.finish_reason,
                prompt_tokens=response.token_budget.prompt_tokens,
                completion_tokens=response.token_budget.completion_tokens,
                total_tokens=response.token_budget.total_tokens,
                requested_max_answer_tokens=response.token_budget.requested_max_answer_tokens,
            )
    except Exception:
        logger.exception("rag_answers telemetry write for /v1/answer failed (non-fatal)")


__all__ = ["router"]
