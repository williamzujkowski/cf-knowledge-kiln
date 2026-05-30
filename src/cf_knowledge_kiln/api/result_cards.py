"""Result-card builder + query-term highlighter for the HTMX UI.

Extracted from :mod:`cf_knowledge_kiln.api.web` (issue #391) so
both the HTMX POST handler (web.py) and the URL-state GET handler
(web_url_state.py) share a single source of truth for the
template-friendly card shape — and so web.py can stay close to the
400-line AGENTS soft cap.

The two functions here are intentionally template-coupled — the
returned dict keys mirror what ``_results.html`` reads. Keep this
module Jinja-free (no template rendering, just data shaping) so a
future swap of presentation layer doesn't require touching it.
"""

from __future__ import annotations

import re
from typing import Any

from markupsafe import Markup, escape

from cf_knowledge_kiln.api.views import (
    authority_tooltip,
    deprecation_label,
    score_tier,
    status_tooltip,
)

# ≥2 to keep domain acronyms (CF, DB, OS, AI) without highlighting
# noisy 1-letter matches. Common 2-letter stopwords are filtered
# explicitly — small list, narrow ambition.
_STOPWORDS: frozenset[str] = frozenset(
    {
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
)

# #291: subsequence-alternation regex is O(N²) in query length. The
# cap guards the tail of that curve — past it, the phrase pass is
# skipped and per-term highlighting carries the result. Generous
# enough that no realistic human query trips it.
_PHRASE_TERM_CAP: int = 12


def result_card_view(
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
        "excerpt_html": highlight_excerpt(excerpt, query),
        # #121: full-text variant for the `o` expand toggle. Same
        # highlighting pass so the marks stay aligned. When content
        # is already ≤500 chars, this is identical to excerpt_html
        # and the toggle is a visual no-op.
        "excerpt_full_html": highlight_excerpt(content, query),
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
        # #336 authority band. Prefer the document-level value
        # (DocumentRef.authority) and fall back to the per-chunk one
        # if the ref didn't carry it. ``None`` for unannotated docs
        # so the template can ``{% if r.authority %}`` cleanly.
        "authority": getattr(ref, "authority", None) or getattr(chunk, "authority", None),
        # #336 editorial gloss for known authority values, mirroring
        # ``status_tooltip``. None for unknown values so the template
        # ``{% if %}``s the data-tooltip / aria-label attributes.
        "authority_tooltip": authority_tooltip(
            getattr(ref, "authority", None) or getattr(chunk, "authority", None)
        ),
        # #337 / #384: surface section position so the HTMX template
        # renders "section N of M" inline. The JSON ResultCard shape
        # carries the same fields (see ``api/retrieval.py``); without
        # also threading them here the HTMX flow would silently lose
        # the section line that #385 added.
        "chunk_index": getattr(chunk, "chunk_index", None),
        # ``chunk.chunk_count`` defaults to 0 for synthetic chunks
        # (tests / mocks); surface 0 as None so the template treats
        # it as "unknown" and falls back to bare "section N".
        "chunk_count": getattr(chunk, "chunk_count", None) or None,
    }


def highlight_excerpt(text: str, query: str) -> Markup:
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
    terms = [
        t for t in re.split(r"\s+", query.strip()) if len(t) >= 2 and t.lower() not in _STOPWORDS
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


__all__ = ["highlight_excerpt", "result_card_view"]
