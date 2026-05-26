"""Unit tests for ``api.views.feedback_categories`` (#278).

The feedback widget renders six terse italic buttons (yes · no ·
stale · …). The label is what sighted users read; the tooltip is
what they get on hover; the aria-label is what AT users hear. All
three flow from a single Python tuple so the engine's signal enum
(``forms.FEEDBACK_TYPES``), the visible UI, and the AT
announcement can't drift apart.
"""

from __future__ import annotations

from cf_knowledge_kiln.api.forms import FEEDBACK_TYPES
from cf_knowledge_kiln.api.views import feedback_categories


class TestFeedbackCategories:
    """Pin the (signal, label, tooltip) contract."""

    def test_returns_six_categories(self) -> None:
        """Six is the canonical count — matches FEEDBACK_TYPES.

        A future addition or removal MUST land in both the engine
        enum and this helper at the same time; this assertion catches
        drift on either side."""
        cats = feedback_categories()
        assert len(cats) == 6
        assert len(cats) == len(FEEDBACK_TYPES)

    def test_signal_values_match_engine_enum(self) -> None:
        """Every category's signal value is a member of the engine's
        FEEDBACK_TYPES enum. A typo here would render a button whose
        POST body the server rejects."""
        signals = {signal for signal, _label, _tooltip in feedback_categories()}
        assert signals == FEEDBACK_TYPES

    def test_each_category_has_short_label_and_explanation(self) -> None:
        """Labels stay terse (the editorial design); tooltips carry
        the full explanation. The shapes diverge on purpose."""
        for signal, label, tooltip in feedback_categories():
            assert label, f"signal {signal!r} has empty label"
            assert tooltip, f"signal {signal!r} has empty tooltip"
            # Labels are short — the design point is a card-foot
            # inline phrase, not a help text block.
            assert len(label) <= 16, f"label too long for inline rendering: {label!r}"
            # Tooltips are sentence-shaped (capitalized, period).
            assert tooltip[0].isupper(), f"tooltip not sentence-cased: {tooltip!r}"
            assert tooltip.endswith("."), f"tooltip missing terminal period: {tooltip!r}"

    def test_useful_and_not_useful_lead_the_list(self) -> None:
        """Editorial order matches the user's likely click sequence:
        the binary 'yes/no' lead; the four diagnostic categories
        follow. A future re-order that puts 'duplicate' first would
        bury the primary signal."""
        cats = feedback_categories()
        assert cats[0][0] == "useful"
        assert cats[1][0] == "not_useful"

    def test_aria_announcement_combines_label_and_tooltip(self) -> None:
        """For each category, the aria-label the template renders is
        ``"{label}: {tooltip}"`` — AT users hear the visible label
        AND the explanation. This pins the contract on the helper
        (the template-level test in tests/unit/test_feedback_widget
        pins the rendered output)."""
        for _signal, label, tooltip in feedback_categories():
            aria = f"{label}: {tooltip}"
            # No double-punctuation drift between label and tooltip.
            assert "::" not in aria
            assert ":  " not in aria
