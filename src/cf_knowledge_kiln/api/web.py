"""HTMX-on-FastAPI server-rendered UI routes (Phase 6, issue #23).

* ``GET /`` returns the full search page shell.
* ``POST /search`` is the HTMX target — accepts form data and returns
  just the results-list HTML fragment so HTMX can swap it into the
  page without a reload.

Related route modules (all mounted by the app factory):

* ``GET /search`` — :mod:`cf_knowledge_kiln.api.web_url_state` (#371)
* ``POST /feedback`` — :mod:`cf_knowledge_kiln.api.web_feedback` (#391)
* ``GET /preview/{chunk_id}`` — :mod:`cf_knowledge_kiln.api.preview`

Form-parsing helpers live in :mod:`cf_knowledge_kiln.api.forms`;
the shared Jinja2 instance lives in :mod:`cf_knowledge_kiln.api._templates`;
the result-card builder lives in :mod:`cf_knowledge_kiln.api.result_cards`.

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
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api._templates import templates
from cf_knowledge_kiln.api.auth import username_for
from cf_knowledge_kiln.api.dependencies import (
    get_hybrid_retriever,
    get_search_limiter,
    get_session,
    get_trust_xff,
)
from cf_knowledge_kiln.api.forms import (
    DEFAULT_STATUSES,
    empty_filters_view,
    filters_from_form,
    selected_statuses,
)
from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter, client_ip
from cf_knowledge_kiln.api.request_id import request_id_for
from cf_knowledge_kiln.api.result_cards import result_card_view
from cf_knowledge_kiln.api.views import (
    humanize_warning,
    log_human_query,
    rail_filters_active_count,
    split_warnings,
)
from cf_knowledge_kiln.retrieval import HybridRetriever
from cf_knowledge_kiln.retrieval.types import MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"], include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def search_page(request: Request) -> HTMLResponse:
    """Render the search page shell. No query yet → empty results."""
    filters_view = empty_filters_view()
    # #273: filters are always empty on this entry, so the count is
    # 0; the template renders the rail closed with no badge. Threaded
    # uniformly with the POST path so a future URL-state restore PR
    # only has to populate filters_view to light up the open + badge.
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": "",
            "initial_results": None,
            "filters": filters_view,
            "rail_active_count": rail_filters_active_count(filters_view),
            # #371: status checkboxes consult ``selected_statuses`` to
            # decide ``checked``. On the empty shell, default to the
            # same set the engine uses when no filter is set
            # (DEFAULT_STATUSES) so the rendered form matches what
            # POST /search would do.
            "selected_statuses": list(DEFAULT_STATUSES),
        },
    )


# NOTE: ``GET /search`` lives in :mod:`cf_knowledge_kiln.api.web_url_state`
# (issue #371) — extracted so this module stays close to the 400-line
# AGENTS soft cap. The app factory mounts both routers.


@router.post("/search", response_class=HTMLResponse)
async def search_partial(
    request: Request,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_search_limiter)],
    trust_xff: Annotated[bool, Depends(get_trust_xff)],
    query: Annotated[str, Form()] = "",
    status: Annotated[list[str] | None, Form()] = None,
    filters_set: Annotated[str | None, Form(alias="_filters_set")] = None,
    # #118: expanded filter rail. Each is optional and skipped when
    # empty so the form behaves identically without the rail expanded.
    repo: Annotated[str, Form()] = "",
    doc_type: Annotated[list[str] | None, Form()] = None,
    owner: Annotated[str, Form()] = "",
    last_reviewed_after: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    # #120: HTMX attaches X-Kiln-Source: keyup-debounce when the
    # search fires from a debounced keystroke. We skip the
    # rag_queries telemetry write for those so a 10-character query
    # doesn't produce 10 stored rows. Explicit submits (button click
    # + Enter) arrive without the header and persist normally.
    kiln_source: Annotated[str | None, Header(alias="X-Kiln-Source")] = None,
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
    key = client_ip(request, trust_xff=trust_xff)
    if not limiter.hit(key):
        # HTMX-friendly fragment with the same template the error path uses.
        retry = limiter.retry_after(key)
        # #340 editorial copy: label gives the operator a clear
        # one-line failure name; message keeps the actionable
        # retry-after detail.
        return templates.TemplateResponse(
            request,
            "_error.html",
            {
                "label": "Too many requests",
                "message": f"Please retry in {retry}s.",
            },
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    query = query.strip()
    if not query:
        return templates.TemplateResponse(
            request, "_results.html", {"results": [], "warnings": [], "query": ""}
        )
    if len(query) > MAX_QUERY_LENGTH:
        # The JSON API rejects this at the Pydantic layer; the HTMX form
        # bypasses SearchRequest, so guard here too — an over-long query
        # forces unbounded FTS + embedding compute. Same error fragment
        # the rate-limit + retrieval-failure paths use.
        return templates.TemplateResponse(
            request,
            "_error.html",
            {
                "label": "Query too long",
                "message": f"Limit is {MAX_QUERY_LENGTH} characters.",
            },
            status_code=413,
        )
    fs = filters_set is not None
    if fs and not selected_statuses(status):
        # User explicitly unchecked every status filter — short-circuit
        # to zero results without hitting the engine. (build_predicates
        # treats an empty status list as "no constraint", which would
        # surprise the user here.)
        return templates.TemplateResponse(
            request, "_results.html", {"results": [], "warnings": [], "query": query}
        )
    filters = filters_from_form(
        status,
        filters_set=fs,
        repo=repo,
        doc_type=doc_type,
        owner=owner,
        last_reviewed_after=last_reviewed_after,
        tags=tags,
    )
    try:
        result = await retriever.search(query, filters=filters, max_results=20, session=session)
    except Exception:
        logger.exception("hybrid retrieval failed for /search query")
        return templates.TemplateResponse(
            request,
            "_error.html",
            {
                # ASCII apostrophe in source; the template can smart-
                # quote on render if desired (ruff RUF001 forbids the
                # curly mark in Python string literals).
                "label": "Couldn't reach the engine",
                "message": "Search is temporarily unavailable; try again in a moment.",
            },
            status_code=503,
        )
    if kiln_source == "keyup-debounce":
        # Skip telemetry for incremental keystrokes — only persist
        # explicit submissions. The feedback widget needs a query_id
        # to bind to, so it won't render on these debounced fragments,
        # which is acceptable: a user clicking thumbs-up has stopped
        # typing and will see a fresh fragment with telemetry on the
        # next submit.
        query_id: object | None = None
    else:
        query_id = await log_human_query(
            session,
            query=query,
            filters=filters,
            chunk_ids=[c.chunk_id for c in result.chunks],
            request_id=request_id_for(request),
            requester=username_for(request),
        )
    cards = [
        result_card_view(
            c,
            result.document_refs.get(c.document_id),
            result.chunk_text.get(c.chunk_id, ""),
            query=query,
        )
        for c in result.chunks
    ]
    # #257: split warnings into query-global vs per-document so the
    # template can render per-document warnings inline on each card.
    # The spec (user-journeys.md:59-69) requires per-result warning
    # context — a security engineer scanning a list shouldn't have to
    # cross-reference a top-of-list 'stale_source' warning with the
    # right card.
    all_warnings = [humanize_warning(w) for w in (result.warnings or [])]
    visible_doc_ids = {str(c.document_id) for c in result.chunks}
    global_warnings, per_doc_warnings = split_warnings(all_warnings, visible_doc_ids)
    for card in cards:
        card["warnings"] = per_doc_warnings.get(str(card.get("document_id")), [])
    return templates.TemplateResponse(
        request,
        "_results.html",
        {
            "query": query,
            "results": cards,
            "warnings": global_warnings,
            "query_id": str(query_id) if query_id else None,
            # The rail isn't re-rendered in the HTMX partial today, but
            # passing the values through keeps the contract symmetric
            # so a future partial can include it.
            "filters": {
                "repo": repo,
                "doc_type": doc_type or [],
                "owner": owner,
                "last_reviewed_after": last_reviewed_after,
                "tags": tags,
            },
            # #123: surface the actually-applied statuses so the
            # no-results fragment can offer one-click widen buttons
            # for statuses not currently selected.
            "selected_statuses": list(filters.status or []),
        },
    )


__all__ = ["router"]
