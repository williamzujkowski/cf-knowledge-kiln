"""Unit tests for ``api.views.rail_filters_active_count`` (#273).

The helper counts how many of the rail's optional filter fields
carry a non-empty value. The template uses the count to:

* render ``<details open>`` when count > 0 (no active filter is
  ever hidden behind a collapsed rail), and
* append "· N active" to the summary label so a scan of the
  closed rail also shows the count.

A filter field is "active" iff its value would be sent to the
engine as a constraint — i.e., a non-empty string or a non-empty
list. Empty strings, missing keys, ``None``, and empty lists all
count as inactive (mirrors :func:`forms.filters_from_form` which
turns each of those into ``None``).
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.api.views import rail_filters_active_count


class TestRailFiltersActiveCount:
    """Pin the active-filter detection rules."""

    def test_all_empty_returns_zero(self) -> None:
        """Default render path: empty_filters_view() shape → 0."""
        view = {
            "repo": "",
            "doc_type": [],
            "owner": "",
            "last_reviewed_after": "",
            "tags": "",
        }
        assert rail_filters_active_count(view) == 0

    @pytest.mark.parametrize(
        "field",
        ["repo", "owner", "last_reviewed_after", "tags"],
    )
    def test_single_string_field_set_counts_one(self, field: str) -> None:
        """Each of the four string fields counts once when populated."""
        view = {
            "repo": "",
            "doc_type": [],
            "owner": "",
            "last_reviewed_after": "",
            "tags": "",
            field: "some-value",
        }
        assert rail_filters_active_count(view) == 1

    def test_doc_type_with_single_value_counts_one(self) -> None:
        """``doc_type`` is a list — a non-empty list counts once
        regardless of how many values it carries (matches the user's
        mental model: 'doc_type filter is active' is one filter).
        """
        view = {
            "repo": "",
            "doc_type": ["runbook"],
            "owner": "",
            "last_reviewed_after": "",
            "tags": "",
        }
        assert rail_filters_active_count(view) == 1

    def test_doc_type_with_many_values_counts_one(self) -> None:
        view = {
            "repo": "",
            "doc_type": ["runbook", "adr", "guide"],
            "owner": "",
            "last_reviewed_after": "",
            "tags": "",
        }
        assert rail_filters_active_count(view) == 1

    def test_doc_type_empty_list_counts_zero(self) -> None:
        view = {
            "repo": "",
            "doc_type": [],
            "owner": "",
            "last_reviewed_after": "",
            "tags": "",
        }
        assert rail_filters_active_count(view) == 0

    def test_multiple_fields_sum_to_their_count(self) -> None:
        """All five fields set → 5 (no double-counting, no skips)."""
        view = {
            "repo": "platform",
            "doc_type": ["runbook"],
            "owner": "alice",
            "last_reviewed_after": "2026-01-01",
            "tags": "auth,sso",
        }
        assert rail_filters_active_count(view) == 5

    def test_missing_keys_treated_as_inactive(self) -> None:
        """A partial dict (some keys missing) doesn't KeyError —
        the helper must be safe against future template changes that
        drop a field or against a test fixture that omits one."""
        assert rail_filters_active_count({"repo": "platform"}) == 1
        assert rail_filters_active_count({}) == 0

    def test_none_values_treated_as_inactive(self) -> None:
        """Belt-and-braces: a None value (from a serializer pass that
        replaces empty with None) counts as inactive, not as a
        truthy presence."""
        view = {
            "repo": None,
            "doc_type": None,
            "owner": None,
            "last_reviewed_after": None,
            "tags": None,
        }
        assert rail_filters_active_count(view) == 0

    def test_whitespace_only_string_treated_as_inactive(self) -> None:
        """``forms.split_csv("   ")`` returns [], so the engine would
        see no constraint. Mirror that behavior here so the visual
        signal matches what's actually sent to retrieval."""
        view = {
            "repo": "   ",
            "doc_type": [],
            "owner": "\t\n",
            "last_reviewed_after": "",
            "tags": "  ",
        }
        assert rail_filters_active_count(view) == 0
