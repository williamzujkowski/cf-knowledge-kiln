"""HTMX ``POST /feedback`` route — writes a rag_feedback row, returns the ack chip.

Extracted from :mod:`cf_knowledge_kiln.api.web` (issue #391) so web.py
can stay close to the 400-line AGENTS soft cap. Both the route and its
error helpers live here; the app factory mounts this router alongside
the others.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api._templates import templates
from cf_knowledge_kiln.api.dependencies import (
    get_feedback_limiter,
    get_session,
    get_trust_xff,
)
from cf_knowledge_kiln.api.forms import (
    FEEDBACK_COMMENT_MAX_LEN,
    FEEDBACK_TYPES,
    parse_uuid,
)
from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter, client_ip
from cf_knowledge_kiln.db.repositories import FeedbackRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"], include_in_schema=False)


@router.post("/feedback", response_class=HTMLResponse)
async def submit_feedback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_feedback_limiter)],
    trust_xff: Annotated[bool, Depends(get_trust_xff)],
    query_id: Annotated[str, Form()],
    chunk_id: Annotated[str, Form()],
    signal: Annotated[str, Form()],
    comment: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """HTMX target — append a rag_feedback row, return an ack chip.

    The form submits inline from a result card; the response HTML
    replaces the form so the user sees a "Thanks!" chip without a
    page reload. Validation errors return a small inline error so
    the user can correct + resubmit.

    NOTE — no CSRF token here. Defensible:

    * /feedback writes operator telemetry, not user account state.
    * A cross-origin POST can pollute the signal stream with noise
      but cannot corrupt records, trigger XSS, or escalate privileges
      (autoescape + FK ON DELETE SET NULL + savepoint isolation).
    * Phase 8 bearer auth (#77) protects the API in production.
    * Per-IP rate limit (issue #79) caps feedback noise.
    """
    key = client_ip(request, trust_xff=trust_xff)
    if not limiter.hit(key):
        retry = limiter.retry_after(key)
        return _feedback_error_with_status(
            request,
            f"Too many feedback submissions. Try again in {retry}s.",
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    if signal not in FEEDBACK_TYPES:
        return _feedback_error(request, "Unknown feedback type.")
    qid = parse_uuid(query_id)
    cid = parse_uuid(chunk_id)
    if qid is None or cid is None:
        return _feedback_error(request, "Invalid query or chunk reference.")
    note = (comment or "").strip()[:FEEDBACK_COMMENT_MAX_LEN] or None
    try:
        async with session.begin_nested():
            await FeedbackRepository(session).create(
                signal=signal,
                query_id=qid,
                chunk_id=cid,
                comment=note,
                source="web",
            )
    except Exception:
        logger.exception("rag_feedback write failed")
        return _feedback_error(request, "Could not record feedback. Try again later.")
    return templates.TemplateResponse(
        request, "_feedback_ack.html", {"signal": signal}, status_code=200
    )


def _feedback_error(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_feedback_error.html", {"message": message}, status_code=400
    )


def _feedback_error_with_status(
    request: Request,
    message: str,
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_feedback_error.html",
        {"message": message},
        status_code=status_code,
        headers=headers,
    )


__all__ = ["router", "submit_feedback"]
