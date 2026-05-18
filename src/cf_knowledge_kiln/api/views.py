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


def humanize_warning(w: Any) -> dict[str, str]:
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


__all__ = ["humanize_warning", "log_human_query"]
