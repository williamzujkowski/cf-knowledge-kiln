"""HTMX-on-FastAPI server-rendered UI routes (Phase 6, issue #23).

* ``GET /`` returns the full search page.
* ``POST /search`` is the HTMX target — accepts form data and returns
  just the results-list HTML fragment so HTMX can swap it into the
  page without a reload.
* ``POST /feedback`` writes a rag_feedback row and returns the inline
  ack chip.

The ``GET /preview/{chunk_id}`` route lives in
:mod:`cf_knowledge_kiln.api.preview` and form-parsing helpers live in
:mod:`cf_knowledge_kiln.api.forms`; both are mounted by the app
factory alongside this router.

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
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.auth import username_for
from cf_knowledge_kiln.api.dependencies import (
    get_feedback_limiter,
    get_hybrid_retriever,
    get_search_limiter,
    get_session,
    get_trust_xff,
)
from cf_knowledge_kiln.api.forms import (
    FEEDBACK_COMMENT_MAX_LEN,
    FEEDBACK_TYPES,
    empty_filters_view,
    filters_from_form,
    parse_uuid,
    selected_statuses,
)
from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter, client_ip
from cf_knowledge_kiln.api.request_id import request_id_for
from cf_knowledge_kiln.api.views import (
    agent_guide_url,
    deprecation_label,
    feedback_categories,
    humanize_warning,
    log_human_query,
    rail_filters_active_count,
    score_tier,
    split_warnings,
    status_tooltip,
)
from cf_knowledge_kiln.db.repositories import FeedbackRepository
from cf_knowledge_kiln.retrieval import HybridRetriever
from cf_knowledge_kiln.retrieval.types import MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# #278: register the feedback-categories helper as a Jinja global so
# the included ``_feedback_widget.html`` partial can iterate it from
# any render context without each route having to thread it through
# the per-call dict.
templates.env.globals["feedback_categories"] = feedback_categories
# #314: agent guide URL helper for the colophon link. Returns None
# when KILN_AGENT_GUIDE_URL is unset, in which case the template
# conditional skips rendering the link entirely.
templates.env.globals["agent_guide_url"] = agent_guide_url

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
        },
    )


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
    if len(query) > MAX_QUERY_LENGTH:
        # The JSON API rejects this at the Pydantic layer; the HTMX form
        # bypasses SearchRequest, so guard here too — an over-long query
        # forces unbounded FTS + embedding compute. Same error fragment
        # the rate-limit + retrieval-failure paths use.
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"message": f"Query too long — limit is {MAX_QUERY_LENGTH} characters."},
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
            {"message": "Search is temporarily unavailable. Please try again."},
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
        _result_card_view(
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


# ─── /feedback ──────────────────────────────────────────────────────


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
    * Per-IP rate limit (issue #79) caps feedback noise; see below.
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


# ─── presentation helpers (template-coupled) ────────────────────────


def _result_card_view(
    chunk: Any, ref: object | None, content: str, query: str = ""
) -> dict[str, Any]:
    """Build a template-friendly dict for one result.

    Mirrors the JSON :class:`ResultCard` shape but keeps templates
    Pydantic-free (Jinja accesses dict keys, not attrs).

    ``excerpt_html`` carries the query-highlighted excerpt as a
    :class:`markupsafe.Markup` value so Jinja autoescape leaves the
    ``<mark>`` tags alone but escapes the surrounding text. ``query``
    is required to highlight; passing empty string is a no-op.
    """
    excerpt = content[:500]
    heading_path_list = list(chunk.heading_path) or None
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "title": getattr(ref, "title", None) or "(unknown)",
        "excerpt": excerpt,
        "excerpt_html": _highlight_excerpt(excerpt, query),
        # #121: full-text variant for the `o` expand toggle. Same
        # highlighting pass so the marks stay aligned. When content
        # is already ≤500 chars, this is identical to excerpt_html
        # and the toggle is a visual no-op.
        "excerpt_full_html": _highlight_excerpt(content, query),
        "heading_path": heading_path_list,
        # #121: " > "-joined for the `c` copy-citation data attribute.
        # Empty string when no heading path so the JS can omit "#" cleanly.
        "heading_path_str": " > ".join(heading_path_list) if heading_path_list else "",
        "repo": getattr(ref, "repo", None),
        "path": getattr(ref, "path", None),
        "source_url": getattr(ref, "source_url", None),
        "owner": getattr(ref, "owner", None),
        "status": chunk.status,
        "last_reviewed": chunk.last_reviewed,
        "score": chunk.score,
        # #259 5-dot visualization tier. The Jinja template renders the
        # dots from this integer instead of recomputing the threshold
        # ladder per cell, so the tier policy lives in one Python
        # function (api.views.score_tier) that's unit-tested.
        "score_tier": score_tier(chunk.score),
        # #268 editorial stamp text for non-current statuses. None for
        # active/approved/draft so the template can conditionally
        # render the stamp without a per-status switch.
        "deprecation_label": deprecation_label(chunk.status),
        # #280 hover/AT tooltip explaining the color-coded badge.
        # None for corpus-native statuses outside the kiln-recommended
        # vocabulary so the template can ``{% if %}`` the attributes.
        "status_tooltip": status_tooltip(chunk.status),
    }


def _highlight_excerpt(text: str, query: str) -> Markup:
    """Wrap each query term in ``<mark>`` and return a Markup-safe value.

    Whole-word case-insensitive match. Terms shorter than 3 chars are
    dropped to avoid highlighting noise on stopwords / one-letter
    matches. ``text`` is HTML-escaped first; the inserted ``<mark>``
    tags are the only literal HTML.

    Returns a :class:`markupsafe.Markup` so the template can render
    ``{{ r.excerpt_html }}`` (no ``|safe`` filter needed) and the
    surrounding text stays autoescaped.
    """
    if not query:
        # escape() already returns Markup; no need to re-wrap.
        return escape(text)
    # ≥2 to keep domain acronyms (CF, DB, OS, AI) without highlighting
    # noisy 1-letter matches. Common 2-letter stopwords are filtered
    # explicitly — small list, narrow ambition.
    _stopwords = {
        "a",
        "an",
        "the",
        "is",
        "of",
        "to",
        "in",
        "on",
        "or",
        "by",
        "at",
        "as",
        "if",
        "it",
        "be",
        "do",
    }
    terms = [
        t for t in re.split(r"\s+", query.strip()) if len(t) >= 2 and t.lower() not in _stopwords
    ]
    if not terms:
        return escape(text)
    escaped = str(escape(text))
    # #291: build a regex alternation that tries contiguous
    # subsequences of ≥2 query terms FIRST (longest-first), then
    # falls back to individual terms. When the excerpt contains the
    # full phrase, the longest alternative wins at that scan position
    # and a single wrapping <mark> covers the whole span — reads as
    # 'this is the phrase you searched for' instead of three
    # unrelated terms that happened to land near each other.
    #
    # Whitespace BETWEEN phrase terms is matched as \s+ so the
    # phrase regex still works across tabs / newlines / multi-spaces
    # the autoescape preserves verbatim.
    #
    # Subsequences are O(N²) in query length — for typical 2-5 term
    # queries that's 6-15 alternatives, well within regex budget. A
    # 50-term adversarial query would generate 1,275 alternatives and
    # blow regex-compile time (~500ms). _PHRASE_TERM_CAP guards the
    # tail of that curve — past the cap, the phrase pass is skipped
    # and per-term highlighting carries the result. The cap is
    # generous enough that no realistic human query trips it.
    _PHRASE_TERM_CAP = 12
    alts: list[str] = []
    if 2 <= len(terms) <= _PHRASE_TERM_CAP:
        # Longest subsequences first so the leftmost-longest match
        # rule of `|` alternation produces the maximum-span mark.
        for length in range(len(terms), 1, -1):
            for start in range(len(terms) - length + 1):
                subseq = terms[start : start + length]
                alts.append(r"\s+".join(re.escape(t) for t in subseq))
    # Individual terms come last so per-term matches only happen
    # where no subsequence matched at that scan position.
    alts.extend(re.escape(t) for t in terms)
    pattern = re.compile("(" + "|".join(alts) + ")", re.IGNORECASE)
    # The only literal HTML we inject is the <mark> tag. Everything
    # else flowing through this Markup() is the output of escape(),
    # so the result is XSS-safe by construction.
    return Markup(  # noqa: S704
        pattern.sub(r"<mark>\1</mark>", escaped)
    )  # nosec B704


__all__ = ["router"]
