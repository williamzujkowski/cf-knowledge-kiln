"""Unit tests for ``api.views.status_tooltip`` (#280).

The status badge on each result card is color-coded (teal for
active/approved, gold for draft, oxblood for the deprecated set).
A new or color-blind user reading the badge has no way to recover
the semantic from the color alone. The helper returns the
editorial gloss the template renders into ``title`` + the AT
announcement.

Mirrors the pattern in :func:`api.views.deprecation_label` —
returns ``None`` for an unknown status so the template can ``{%
if %}`` the attribute on without a per-status switch.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.api.views import status_tooltip


class TestStatusTooltip:
    """Pin the (status → tooltip) contract."""

    def test_active_status(self) -> None:
        assert status_tooltip("active") == "Current — the canonical version."

    def test_approved_status(self) -> None:
        assert status_tooltip("approved") == "Approved — reviewed and signed off."

    def test_draft_status(self) -> None:
        assert status_tooltip("draft") == "Draft — not yet approved as authoritative."

    def test_deprecated_status(self) -> None:
        assert status_tooltip("deprecated") == "Deprecated — superseded; do not cite as current."

    def test_archived_status(self) -> None:
        assert status_tooltip("archived") == "Archived — kept for historical reference."

    def test_superseded_status(self) -> None:
        assert status_tooltip("superseded") == "Superseded — see the linked successor."

    def test_unknown_status_returns_none(self) -> None:
        """Corpus-native statuses outside the kiln-recommended set
        (e.g. 'reference', 'canonical', 'running' — per the #203
        open-status model) get no tooltip. Better to omit than to
        guess a meaning the operator never wrote down."""
        assert status_tooltip("reference") is None
        assert status_tooltip("canonical") is None
        assert status_tooltip("running") is None
        assert status_tooltip("") is None

    @pytest.mark.parametrize(
        "status",
        ["active", "approved", "draft", "deprecated", "archived", "superseded"],
    )
    def test_tooltip_copy_is_sentence_shaped(self, status: str) -> None:
        """All tooltips are single-sentence, capitalized, period-
        terminated — matches the editorial voice of the deprecation
        labels and feedback tooltips."""
        tooltip = status_tooltip(status)
        assert tooltip is not None
        assert tooltip[0].isupper(), f"not sentence-cased: {tooltip!r}"
        assert tooltip.endswith("."), f"missing terminal period: {tooltip!r}"

    @pytest.mark.parametrize(
        "status",
        ["active", "approved", "draft", "deprecated", "archived", "superseded"],
    )
    def test_tooltip_leads_with_status_word(self, status: str) -> None:
        """Each tooltip leads with the Title-cased status word
        ('Current' / 'Approved' / 'Draft' / ...) so the AT
        announcement reads as 'deprecated: Deprecated — …' rather
        than 'deprecated: …', echoing the visible label for context."""
        tooltip = status_tooltip(status)
        assert tooltip is not None
        # 'active' is the exception — the gloss reads 'Current — …' to
        # avoid the awkward 'Active — active.' redundancy.
        if status == "active":
            assert tooltip.startswith("Current"), tooltip
        else:
            assert tooltip.lower().startswith(status), tooltip
