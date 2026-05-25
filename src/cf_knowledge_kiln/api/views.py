"""View-shaping helpers for the HTMX search results (#129).

Extracted from :mod:`cf_knowledge_kiln.api.web` so the route module
stays under the AGENTS.md 400-line soft cap. These helpers turn
engine outputs into shapes the Jinja templates consume — they don't
own routing or form parsing.

Public surface:

* :func:`humanize_warning` — engine :class:`Warning` to ``{prefix, message, type}``
* :func:`log_human_query` — best-effort ``rag_queries`` telemetry write
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.db.repositories import QueriesRepository
from cf_knowledge_kiln.retrieval import RetrievalFilters

logger = logging.getLogger(__name__)

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


def humanize_warning(w: Any) -> dict[str, Any]:
    """Map an engine :class:`Warning` to a template-ready dict.

    Returns ``{type, prefix, message, source_id, severity}``.

    Falls back to the engine's raw ``message`` when the warning type
    isn't in the spec-mandated copy table (so a future warning type
    surfaces something rather than nothing).

    An empty override string in :data:`_WARNING_COPY` is intentional:
    the prefix carries the spec-mandated voice, and the engine's raw
    message carries the per-instance detail (e.g., \"Document last
    reviewed 2024-01-15\"). The two together read as a margin note.

    ``source_id`` (#257) flows through so the route can route
    per-document warnings to inline per-card rendering; query-global
    warnings (those without a source_id) stay at the top of the list.
    ``severity`` (#257) is the visual-treatment bucket the template
    uses for inline badge styling.
    """
    wtype = getattr(w, "type", "")
    raw = getattr(w, "message", "") or ""
    prefix, override = _WARNING_COPY.get(wtype, ("", raw))
    source_id = getattr(w, "source_id", None)
    return {
        "type": wtype,
        "prefix": prefix,
        "message": override or raw,
        "source_id": str(source_id) if source_id is not None else None,
        "severity": warning_severity(wtype),
    }


# Visual-severity mapping for the human UI (#257). Three buckets:
#
# * ``advisory``  — informational (yellow rule, italic prefix); reading
#   the result is fine, the operator just needs to know.
# * ``warning``   — caution required (oxblood rule, bold prefix); the
#   result is still useful but the operator should weigh the signal.
# * ``blocking``  — refuse-to-act class (oxblood + heavier rule); a
#   sensitive-content / prompt-injection match. The result should NOT
#   be cited without operator review.
#
# Engine-side ``requires_human_review`` already encodes the policy
# decision; this is the UI-side encoding for the inline badges.
_WARNING_SEVERITY: dict[str, str] = {
    "stale_source": "advisory",
    "deprecated_source": "warning",
    "query_normalized": "advisory",
    "weak_evidence": "warning",
    "isolated_match": "warning",
    "conflicting_sources": "warning",
    "prompt_injection_pattern": "blocking",
    "sensitive_content": "blocking",
}


def warning_severity(wtype: str) -> str:
    """Return the visual-severity bucket for ``wtype`` (#257).

    Defaults to ``advisory`` for any unrecognized type so a future
    warning surfaces as a quiet hint until the UI catches up — not
    as a high-stakes red flag.
    """
    return _WARNING_SEVERITY.get(wtype, "advisory")


def split_warnings(
    warnings: list[dict[str, Any]],
    result_document_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Bucket humanized warnings into (query-global, per-document) (#257).

    A warning with a ``source_id`` that matches a visible result's
    document_id is attached to that card; everything else (including
    warnings whose source_id matches a chunk that didn't make it
    into the visible top-K) stays at the top.

    Returns ``(global_warnings, per_document_warnings)`` where the
    per-document map is keyed by ``document_id`` (string form).
    """
    global_warnings: list[dict[str, Any]] = []
    per_doc: dict[str, list[dict[str, Any]]] = {}
    for w in warnings:
        sid = w.get("source_id")
        if sid is not None and sid in result_document_ids:
            per_doc.setdefault(sid, []).append(w)
        else:
            global_warnings.append(w)
    return global_warnings, per_doc


async def log_human_query(
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


__all__ = [
    "humanize_warning",
    "log_human_query",
    "split_warnings",
    "warning_severity",
]
