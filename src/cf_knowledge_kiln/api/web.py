"""HTMX-on-FastAPI server-rendered UI routes (Phase 6, issue #23).

* ``GET /`` returns the full search page.
* ``POST /search`` is the HTMX target — accepts form data and returns
  just the results-list HTML fragment so HTMX can swap it into the
  page without a reload.

These routes intentionally live separately from the JSON API in
``api/retrieval.py``. The JSON API is the canonical interface for
agents + machines; the HTML routes are a thin presentation layer
over the same :class:`HybridRetriever`.

Per AGENTS.md "Deprecated docs must be visibly flagged in results" —
the CSS in ``static/kiln.css`` applies a distinct style + a status
badge to non-active result cards.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.dependencies import (
    get_feedback_limiter,
    get_hybrid_retriever,
    get_search_limiter,
    get_session,
)
from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter, client_ip
from cf_knowledge_kiln.db.repositories import FeedbackRepository, QueriesRepository
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters, Status

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["web"], include_in_schema=False)

# Default status filter shown on initial page load — same heuristic
# as the JSON API's ``KILN_DEFAULT_STATUS_PREFERENCE`` setting.
_DEFAULT_STATUSES: list[Status] = ["active", "approved"]


@router.get("/", response_class=HTMLResponse)
async def search_page(request: Request) -> HTMLResponse:
    """Render the search page shell. No query yet → empty results."""
    return templates.TemplateResponse(
        request,
        "search.html",
        {"query": "", "initial_results": None},
    )


@router.post("/search", response_class=HTMLResponse)
async def search_partial(
    request: Request,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_search_limiter)],
    query: Annotated[str, Form()] = "",
    status: Annotated[list[str] | None, Form()] = None,
    filters_set: Annotated[str | None, Form(alias="_filters_set")] = None,
) -> HTMLResponse:
    """HTMX target — return the results-list fragment.

    The HTMX form on ``search.html`` POSTs here on submit + on
    debounced keyup. We render only the inner-html fragment so HTMX
    swaps it into ``#results`` without reloading the page.

    Error path: if retrieval itself fails (DB down, transient
    pgvector error), render an error fragment so HTMX swaps a
    friendly message into ``#results`` instead of FastAPI's bare
    500 body. Telemetry stays best-effort.
    """
    if not limiter.hit(client_ip(request)):
        # HTMX-friendly fragment with the same template the error path uses.
        retry = limiter.retry_after(client_ip(request))
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"message": f"Too many requests. Please retry in {retry}s."},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    query = query.strip()
    if not query:
        return templates.TemplateResponse(
            request, "_results.html", {"results": [], "warnings": [], "query": ""}
        )
    fs = filters_set is not None
    if fs and not _selected_statuses(status):
        # User explicitly unchecked every status filter — short-circuit
        # to zero results without hitting the engine. (build_predicates
        # treats an empty status list as "no constraint", which would
        # surprise the user here.)
        return templates.TemplateResponse(
            request, "_results.html", {"results": [], "warnings": [], "query": query}
        )
    filters = _filters_from_form(status, filters_set=fs)
    try:
        result = await retriever.search(query, filters=filters, max_results=20, session=session)
    except Exception:
        logger.exception("hybrid retrieval failed for /search query")
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"message": "Search is temporarily unavailable. Please try again."},
            status_code=503,
        )
    query_id = await _log_human_query(
        session, query=query, filters=filters, chunk_ids=[c.chunk_id for c in result.chunks]
    )
    cards = [
        _result_card_view(
            c, result.document_refs.get(c.document_id), result.chunk_text.get(c.chunk_id, "")
        )
        for c in result.chunks
    ]
    return templates.TemplateResponse(
        request,
        "_results.html",
        {
            "query": query,
            "results": cards,
            "warnings": result.warnings or [],
            "query_id": str(query_id) if query_id else None,
        },
    )


# ─── /feedback ──────────────────────────────────────────────────────


_FEEDBACK_TYPES: frozenset[str] = frozenset(
    {
        "useful",
        "not_useful",
        "stale",
        "wrong_source",
        "missing_source",
        "duplicate_or_conflicting",
    }
)
"""Six feedback signals per the plan + issue #25."""

_FEEDBACK_COMMENT_MAX_LEN: int = 500
"""Per-comment character cap. Enforced server-side; no PII guarantees
beyond what the user voluntarily enters."""


@router.post("/feedback", response_class=HTMLResponse)
async def submit_feedback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_feedback_limiter)],
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
    * Per-IP rate limit (issue #79) caps feedback noise; see below.
    """
    if not limiter.hit(client_ip(request)):
        retry = limiter.retry_after(client_ip(request))
        return _feedback_error_with_status(
            request,
            f"Too many feedback submissions. Try again in {retry}s.",
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    if signal not in _FEEDBACK_TYPES:
        return _feedback_error(request, "Unknown feedback type.")
    qid = _parse_uuid(query_id)
    cid = _parse_uuid(chunk_id)
    if qid is None or cid is None:
        return _feedback_error(request, "Invalid query or chunk reference.")
    note = (comment or "").strip()[:_FEEDBACK_COMMENT_MAX_LEN] or None
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


def _parse_uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


# ─── helpers ────────────────────────────────────────────────────────


_VALID_STATUSES: frozenset[str] = frozenset(
    {"active", "approved", "draft", "deprecated", "archived", "superseded"}
)


def _selected_statuses(status: list[str] | None) -> list[str]:
    """Return only the form values that match a real Status enum."""
    return [s for s in (status or []) if s in _VALID_STATUSES]


def _filters_from_form(status: list[str] | None, *, filters_set: bool) -> RetrievalFilters:
    """Translate form checkbox values into :class:`RetrievalFilters`.

    ``filters_set=True`` means the search form actually submitted the
    filter fieldset (the hidden ``_filters_set`` marker arrived). In
    that case an empty ``status`` list is the user's intentional choice
    ("show nothing matching these statuses") and we propagate it.
    Only when ``filters_set`` is False — i.e., a programmatic POST that
    didn't include the marker — do we fall back to
    :data:`_DEFAULT_STATUSES`.

    Unknown status values are dropped silently (the form is closed by
    the template, but a hand-crafted POST might submit anything).
    """
    if filters_set:
        raw: list[str] = status or []
    elif status is not None:
        raw = status
    else:
        raw = list(_DEFAULT_STATUSES)
    selected = [s for s in raw if s in _VALID_STATUSES]
    # When filters_set=True with empty selected, the handler
    # short-circuits before we get here, so this path never returns
    # `status=[]` to the engine (which would be a no-constraint
    # surprise). When filters_set=False, selected is at minimum the
    # _DEFAULT_STATUSES, so `selected or None` always gives a list.
    return RetrievalFilters(status=selected or None)  # type: ignore[arg-type]


def _result_card_view(chunk: Any, ref: object | None, content: str) -> dict[str, Any]:
    """Build a template-friendly dict for one result.

    Mirrors the JSON :class:`ResultCard` shape but keeps templates
    Pydantic-free (Jinja accesses dict keys, not attrs).
    """
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "title": getattr(ref, "title", None) or "(unknown)",
        "excerpt": content[:500],
        "heading_path": list(chunk.heading_path) or None,
        "repo": getattr(ref, "repo", None),
        "path": getattr(ref, "path", None),
        "status": chunk.status,
        "last_reviewed": chunk.last_reviewed,
        "score": chunk.score,
    }


async def _log_human_query(
    session: AsyncSession,
    *,
    query: str,
    filters: RetrievalFilters,
    chunk_ids: list[Any],
) -> object | None:
    """Append a row to ``rag_queries`` and return its id (or None on failure).

    The id is rendered into the result page so the feedback widget
    can tie each rag_feedback row back to the query that produced
    the chunk. Non-fatal: telemetry failure logs + returns None,
    which the template renders without feedback widgets (rather
    than 500'ing the user's search).
    """
    try:
        async with session.begin_nested():
            row = await QueriesRepository(session).create(
                query=query,
                consumer_type="human",
                filters=filters.model_dump(exclude_none=True),
                retrieved_chunk_ids=chunk_ids,
            )
        return row.id
    except Exception:
        logger.exception("rag_queries telemetry write failed (non-fatal)")
        return None


__all__ = ["router"]
