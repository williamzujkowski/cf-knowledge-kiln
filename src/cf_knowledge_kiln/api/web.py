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

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.dependencies import get_hybrid_retriever, get_session
from cf_knowledge_kiln.db.repositories import QueriesRepository
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
    await _log_human_query(
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
        {"query": query, "results": cards, "warnings": result.warnings or []},
    )


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
) -> None:
    """Append a row to ``rag_queries`` (consumer_type='human').

    Same non-fatal pattern as the JSON-API handlers (slice 4 + #75).
    """
    try:
        async with session.begin_nested():
            await QueriesRepository(session).create(
                query=query,
                consumer_type="human",
                filters=filters.model_dump(exclude_none=True),
                retrieved_chunk_ids=chunk_ids,
            )
    except Exception:
        logger.exception("rag_queries telemetry write failed (non-fatal)")


__all__ = ["router"]
