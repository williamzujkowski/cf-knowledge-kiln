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
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.dependencies import (
    get_feedback_limiter,
    get_hybrid_retriever,
    get_search_limiter,
    get_session,
    get_trust_xff,
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
        {"query": "", "initial_results": None, "filters": _empty_filters_view()},
    )


def _empty_filters_view() -> dict[str, Any]:
    r"""Default filter-rail view dict — every field empty/None.

    The template reads dotted keys (\`filters.repo\` etc.) so a flat
    dict with the same keys lets the rail render with no values on
    initial page load.
    """
    return {
        "repo": "",
        "doc_type": [],
        "owner": "",
        "last_reviewed_after": "",
        "tags": "",
    }


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
    fs = filters_set is not None
    if fs and not _selected_statuses(status):
        # User explicitly unchecked every status filter — short-circuit
        # to zero results without hitting the engine. (build_predicates
        # treats an empty status list as "no constraint", which would
        # surprise the user here.)
        return templates.TemplateResponse(
            request, "_results.html", {"results": [], "warnings": [], "query": query}
        )
    filters = _filters_from_form(
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
    query_id = await _log_human_query(
        session, query=query, filters=filters, chunk_ids=[c.chunk_id for c in result.chunks]
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
    return templates.TemplateResponse(
        request,
        "_results.html",
        {
            "query": query,
            "results": cards,
            "warnings": [_humanize_warning(w) for w in (result.warnings or [])],
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


def _filters_from_form(
    status: list[str] | None,
    *,
    filters_set: bool,
    repo: str = "",
    doc_type: list[str] | None = None,
    owner: str = "",
    last_reviewed_after: str = "",
    tags: str = "",
) -> RetrievalFilters:
    """Translate form values into :class:`RetrievalFilters`.

    ``filters_set=True`` means the search form actually submitted the
    filter fieldset (the hidden ``_filters_set`` marker arrived). In
    that case an empty ``status`` list is the user's intentional choice
    ("show nothing matching these statuses") and we propagate it.
    Only when ``filters_set`` is False — i.e., a programmatic POST that
    didn't include the marker — do we fall back to
    :data:`_DEFAULT_STATUSES`.

    Unknown status values are dropped silently (the form is closed by
    the template, but a hand-crafted POST might submit anything).

    #118 adds the expanded rail (repo / doc_type / owner /
    last_reviewed_after / tags). Each is optional — empty input
    becomes ``None`` so the engine sees no constraint.
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
    # status is typed `list[Status]` (a Literal) on the model; we
    # narrow to those values above but mypy doesn't track that.
    return RetrievalFilters(
        status=selected or None,  # type: ignore[arg-type]
        repo=_split_csv(repo) or None,
        doc_type=doc_type or None,
        owner=_split_csv(owner) or None,
        last_reviewed_after=_parse_iso_date(last_reviewed_after),
        tags=_split_csv(tags) or None,
    )


def _split_csv(raw: str) -> list[str]:
    """Split a comma- or whitespace-separated input into a clean list.

    Used for free-text fields (repo, owner, tags) where the form
    accepts either ``foo,bar`` or ``foo bar`` or ``foo, bar``. Empty
    input returns an empty list — caller decides whether that becomes
    ``None``.
    """
    return [t for t in re.split(r"[,\s]+", raw.strip()) if t]


def _parse_iso_date(raw: str) -> Any:
    """Coerce an HTML ``<input type=\"date\">`` value to ``datetime.date``.

    HTML date inputs always submit ISO-8601 (\"YYYY-MM-DD\") so a
    permissive parser isn't needed. Empty input returns ``None``.
    Invalid input also returns ``None`` rather than 422-ing — the
    form-side validator catches malformed values before they reach
    here in normal use.
    """
    if not raw:
        return None
    from datetime import date

    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


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
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "title": getattr(ref, "title", None) or "(unknown)",
        "excerpt": excerpt,
        "excerpt_html": _highlight_excerpt(excerpt, query),
        "heading_path": list(chunk.heading_path) or None,
        "repo": getattr(ref, "repo", None),
        "path": getattr(ref, "path", None),
        "source_url": getattr(ref, "source_url", None),
        "owner": getattr(ref, "owner", None),
        "status": chunk.status,
        "last_reviewed": chunk.last_reviewed,
        "score": chunk.score,
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
    pattern = re.compile(
        r"(" + "|".join(re.escape(t) for t in terms) + r")",
        re.IGNORECASE,
    )
    # The only literal HTML we inject is the <mark> tag. Everything
    # else flowing through this Markup() is the output of escape(),
    # so the result is XSS-safe by construction.
    return Markup(  # noqa: S704
        pattern.sub(r"<mark>\1</mark>", escaped)
    )  # nosec B704


# Spec-mandated warning copy from docs/user-journeys.md, plus a quiet
# italic prefix for the other emitted warning types. The template
# renders {prefix, message} and lets the prefix lead with italic
# voice instead of dropping the engine's raw string into the page.
_WARNING_COPY: dict[str, tuple[str, str]] = {
    "weak_evidence": (
        "Confidence is low —",
        "I found related content, but no clearly authoritative source.",
    ),
    "conflicting_sources": (
        "Sources disagree —",
        "I found multiple sources that may conflict. "
        "Prefer active/approved docs unless you are researching history.",
    ),
    "stale_source": ("Source is stale —", ""),
    "deprecated_source": ("Document is deprecated —", ""),
    "prompt_injection_pattern": ("Caution —", ""),
    "sensitive_content": ("Sensitive content —", ""),
    "query_normalized": ("Query was normalized —", ""),
}


def _humanize_warning(w: Any) -> dict[str, str]:
    """Map an engine :class:`Warning` to ``{prefix, message, type}``.

    Falls back to the engine's raw ``message`` when the warning type
    isn't in the spec-mandated copy table (so a future warning type
    surfaces something rather than nothing).

    An empty override string in :data:`_WARNING_COPY` is intentional:
    the prefix carries the spec-mandated voice, and the engine's raw
    message carries the per-instance detail (e.g., \"Document last
    reviewed 2024-01-15\"). The two together read as a margin note.
    """
    wtype = getattr(w, "type", "")
    raw = getattr(w, "message", "") or ""
    prefix, override = _WARNING_COPY.get(wtype, ("", raw))
    return {"type": wtype, "prefix": prefix, "message": override or raw}


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
