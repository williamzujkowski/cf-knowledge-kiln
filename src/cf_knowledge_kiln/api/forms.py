"""Form-parsing helpers + shared constants for the HTMX web routes (#129).

Extracted from :mod:`cf_knowledge_kiln.api.web` and
:mod:`cf_knowledge_kiln.api.preview` so the route modules stay under the
AGENTS.md 400-line soft cap. These helpers translate raw HTML form
inputs into :class:`RetrievalFilters` and other engine-friendly shapes
without knowing anything about FastAPI or Jinja templates.

Public surface (no leading underscore, since they're consumed across
modules now):

* :data:`DEFAULT_STATUSES`, :data:`VALID_STATUSES`
* :func:`selected_statuses`, :func:`filters_from_form`
* :func:`split_csv`, :func:`parse_iso_date`, :func:`parse_uuid`
* :func:`empty_filters_view`
* :data:`FEEDBACK_TYPES`, :data:`FEEDBACK_COMMENT_MAX_LEN`
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import UUID

from cf_knowledge_kiln.retrieval import RetrievalFilters, Status

# Default status filter shown on initial page load — same heuristic
# as the JSON API's ``KILN_DEFAULT_STATUS_PREFERENCE`` setting.
DEFAULT_STATUSES: list[Status] = ["active", "approved"]

VALID_STATUSES: frozenset[str] = frozenset(
    {"active", "approved", "draft", "deprecated", "archived", "superseded"}
)

FEEDBACK_TYPES: frozenset[str] = frozenset(
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

FEEDBACK_COMMENT_MAX_LEN: int = 500
"""Per-comment character cap. Enforced server-side; no PII guarantees
beyond what the user voluntarily enters."""


def empty_filters_view() -> dict[str, Any]:
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


def selected_statuses(status: list[str] | None) -> list[str]:
    """Return only the form values that match a real Status enum."""
    return [s for s in (status or []) if s in VALID_STATUSES]


def filters_from_form(
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
    :data:`DEFAULT_STATUSES`.

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
        raw = list(DEFAULT_STATUSES)
    selected = [s for s in raw if s in VALID_STATUSES]
    # When filters_set=True with empty selected, the handler
    # short-circuits before we get here, so this path never returns
    # `status=[]` to the engine (which would be a no-constraint
    # surprise). When filters_set=False, selected is at minimum the
    # DEFAULT_STATUSES, so `selected or None` always gives a list.
    # status is typed `list[Status]` (a Literal) on the model; we
    # narrow to those values above but mypy doesn't track that.
    return RetrievalFilters(
        status=selected or None,  # type: ignore[arg-type]
        repo=split_csv(repo) or None,
        doc_type=doc_type or None,
        owner=split_csv(owner) or None,
        last_reviewed_after=parse_iso_date(last_reviewed_after),
        tags=split_csv(tags) or None,
    )


def split_csv(raw: str) -> list[str]:
    """Split a comma- or whitespace-separated input into a clean list.

    Used for free-text fields (repo, owner, tags) where the form
    accepts either ``foo,bar`` or ``foo bar`` or ``foo, bar``. Empty
    input returns an empty list — caller decides whether that becomes
    ``None``.
    """
    return [t for t in re.split(r"[,\s]+", raw.strip()) if t]


def parse_iso_date(raw: str) -> date | None:
    """Coerce an HTML ``<input type=\"date\">`` value to ``datetime.date``.

    HTML date inputs always submit ISO-8601 (\"YYYY-MM-DD\") so a
    permissive parser isn't needed. Empty input returns ``None``.
    Invalid input also returns ``None`` rather than 422-ing — the
    form-side validator catches malformed values before they reach
    here in normal use.
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def parse_uuid(raw: str) -> UUID | None:
    """Best-effort UUID coercion — None for malformed input."""
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


__all__ = [
    "DEFAULT_STATUSES",
    "FEEDBACK_COMMENT_MAX_LEN",
    "FEEDBACK_TYPES",
    "VALID_STATUSES",
    "empty_filters_view",
    "filters_from_form",
    "parse_iso_date",
    "parse_uuid",
    "selected_statuses",
    "split_csv",
]
