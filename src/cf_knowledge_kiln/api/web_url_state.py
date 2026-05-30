"""Issue #371: ``GET /search`` route for URL-shareable filter state.

Split out from :mod:`cf_knowledge_kiln.api.web` so that module stays
manageable (the POST handler + presentation helpers there are large
enough on their own). The GET handler is logically distinct:

* Shares the search rate limiter with POST /search (security review
  found the bare GET path was a DoS vector — same retrieval cost
  per request, no upstream throttle).
* No telemetry write (URL-arrival isn't an authored query).
* Renders the full ``search.html`` page (POST returns a fragment).

Both handlers share the same retrieval pipeline + presentation
shape via :mod:`cf_knowledge_kiln.api.result_cards` and
:mod:`cf_knowledge_kiln.api.views` — the duplication is in the
call-site wiring, not the rendering.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api._templates import templates
from cf_knowledge_kiln.api.dependencies import (
    get_hybrid_retriever,
    get_search_limiter,
    get_session,
    get_trust_xff,
)
from cf_knowledge_kiln.api.forms import (
    DEFAULT_STATUSES,
    filters_from_form,
    selected_statuses,
)
from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter, client_ip
from cf_knowledge_kiln.api.result_cards import result_card_view
from cf_knowledge_kiln.api.views import (
    humanize_warning,
    rail_filters_active_count,
    split_warnings,
)
from cf_knowledge_kiln.retrieval import HybridRetriever
from cf_knowledge_kiln.retrieval.types import MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)

# include_in_schema=False — these are web UI routes, not the JSON API.
router = APIRouter(tags=["web"], include_in_schema=False)


def _filters_view(
    repo: str,
    doc_type: list[str] | None,
    owner: str,
    last_reviewed_after: str,
    tags: str,
) -> dict[str, object]:
    """Template-friendly dict for the filter rail. Same shape as
    :func:`cf_knowledge_kiln.api.forms.empty_filters_view` but with
    the URL-provided values populated. Centralized so the four
    early-return branches in :func:`search_page_from_url` don't drift."""
    return {
        "repo": repo,
        "doc_type": doc_type or [],
        "owner": owner,
        "last_reviewed_after": last_reviewed_after,
        "tags": tags,
    }


@router.get("/search", response_class=HTMLResponse)
async def search_page_from_url(
    request: Request,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[TokenBucketLimiter, Depends(get_search_limiter)],
    trust_xff: Annotated[bool, Depends(get_trust_xff)],
    q: Annotated[str, Query()] = "",
    # Multi-value query params (``?status=a&status=b``) require explicit
    # ``Query()`` annotation; without it, FastAPI picks up only the first
    # value and the URL-shareable filter loses every status after the
    # first. Same shape for ``doc_type``.
    status: Annotated[list[str] | None, Query()] = None,
    repo: Annotated[str, Query()] = "",
    doc_type: Annotated[list[str] | None, Query()] = None,
    owner: Annotated[str, Query()] = "",
    last_reviewed_after: Annotated[str, Query()] = "",
    tags: Annotated[str, Query()] = "",
) -> HTMLResponse:
    """#371: URL-shareable filter state. Same shape as the
    HTMX POST handler, but renders the full ``search.html`` page so
    a copy/pasted URL reproduces the query + filters + results in
    one round-trip.

    Rate limit: shares :func:`get_search_limiter` with POST /search.
    Each request costs one retrieval-pipeline run (vector + FTS),
    same as POST — so the same per-IP token budget applies. Security
    review caught the bare GET path as a DoS vector since arbitrary
    expensive queries could be hammered without throttling.

    Telemetry: not logged. The POST handler persists
    ``rag_queries`` rows for explicit submissions, but a GET arrival
    from a shared URL isn't an authored query — the originating
    submission was already logged on whoever first ran it. Logging
    here would inflate query-volume metrics on every link click.
    """
    key = client_ip(request, trust_xff=trust_xff)
    if not limiter.hit(key):
        retry = limiter.retry_after(key)
        # Plain HTML error page (not the HTMX-friendly fragment the
        # POST handler returns) because this route always renders the
        # full page, not a swappable fragment.
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
    query = q.strip()
    fs = status is not None  # any explicit status param → ``filters_set``
    if not query:
        filters_view = _filters_view(repo, doc_type, owner, last_reviewed_after, tags)
        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "query": "",
                "initial_results": None,
                "filters": filters_view,
                "rail_active_count": rail_filters_active_count(filters_view),
                "selected_statuses": (selected_statuses(status) if fs else list(DEFAULT_STATUSES)),
            },
        )
    if len(query) > MAX_QUERY_LENGTH:
        # 413 even on the GET path — same body shape as POST so the
        # error message is consistent regardless of how the user got
        # here.
        return templates.TemplateResponse(
            request,
            "_error.html",
            {
                "label": "Query too long",
                "message": f"Limit is {MAX_QUERY_LENGTH} characters.",
            },
            status_code=413,
        )
    sel = selected_statuses(status) if fs else list(DEFAULT_STATUSES)
    if fs and not sel:
        # ``?status=`` with only unknown values → render empty shell
        # rather than the engine's "no constraint" default. Same
        # short-circuit as POST /search.
        filters_view = _filters_view(repo, doc_type, owner, last_reviewed_after, tags)
        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "query": query,
                "initial_results": {
                    "cards": [],
                    "warnings": [],
                    "query_id": None,
                    # #371 reviewer-fix: same shape as the success path
                    # so the {% with %} include in search.html doesn't
                    # silently fall through to Jinja's undefined coercion.
                    # _results.html's widen-buttons need this list (even
                    # empty) to decide which one-click options to surface.
                    "selected_statuses": [],
                },
                "filters": filters_view,
                "rail_active_count": rail_filters_active_count(filters_view),
                "selected_statuses": [],
            },
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
        logger.exception("hybrid retrieval failed for GET /search query")
        return templates.TemplateResponse(
            request,
            "_error.html",
            {
                "label": "Couldn't reach the engine",
                "message": "Search is temporarily unavailable; try again in a moment.",
            },
            status_code=503,
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
    all_warnings = [humanize_warning(w) for w in (result.warnings or [])]
    visible_doc_ids = {str(c.document_id) for c in result.chunks}
    global_warnings, per_doc_warnings = split_warnings(all_warnings, visible_doc_ids)
    for card in cards:
        card["warnings"] = per_doc_warnings.get(str(card.get("document_id")), [])
    filters_view = _filters_view(repo, doc_type, owner, last_reviewed_after, tags)
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": query,
            "initial_results": {
                "cards": cards,
                "warnings": global_warnings,
                "query_id": None,
                "selected_statuses": list(filters.status or []),
            },
            "filters": filters_view,
            "rail_active_count": rail_filters_active_count(filters_view),
            "selected_statuses": sel,
        },
    )


__all__ = ["router", "search_page_from_url"]
