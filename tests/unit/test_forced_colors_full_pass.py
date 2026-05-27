"""Pins the #352 forced-colors full pass.

Audit Finding #20: under @media (forced-colors: active) Windows High
Contrast strips every author-set color and substitutes system tokens.
Without explicit rules, several color-bearing surfaces collapse:
* The deprecation stripe (diagonal hatch in author oxblood-tint)
* The gutter rule + stamp on the deprecated card
* The status badge (color-coded teal/gold/oxblood)
* The status pill checked-vs-unchecked state
* The shortcut hint banner background

This partial adds explicit forced-colors rules with system tokens
(CanvasText, Canvas, ButtonText, Highlight, HighlightText, LinkText)
so the affordances stay distinguishable.

Already-covered surfaces (test in their own files):
* Score widget (#354 → _results.css)
* Warning pill glyphs (#355 → _results.css)
* Focus-visible ring (#356 → _base.css)
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PARTIAL = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_forced_colors.css"
_BUNDLE = _REPO / "src/cf_knowledge_kiln/api/static/kiln.css"


def _css() -> str:
    return _PARTIAL.read_text()


class TestPartialExistsAndBundled:
    def test_partial_present(self) -> None:
        assert _PARTIAL.exists(), "_forced_colors.css partial must exist"

    def test_bundle_includes_partial(self) -> None:
        bundle = _BUNDLE.read_text()
        # Specific marker strings that only appear in the new partial.
        assert "#352 Windows High Contrast / forced-colors full pass" in bundle


class TestDeprecationStripeFallback:
    def test_color_only_stripe_replaced_by_border(self) -> None:
        """The author oxblood-stripe is gradient-only and disappears
        under WHC. The fallback is a CanvasText left border that
        guarantees the card is visually flagged."""
        css = _css()
        # The forced-colors rule for deprecated/archived/superseded
        # must REMOVE the background AND add a left border.
        assert ".result-card.status-deprecated" in css
        idx = css.index(".result-card.status-deprecated")
        # Window covers the rule body.
        body = css[idx : idx + 600]
        assert "background: transparent" in body
        assert "border-inline-start: 4px solid CanvasText" in body
        # forced-color-adjust: none lets author rules survive the
        # system override on this specific element.
        assert "forced-color-adjust: none" in body

    def test_deprecation_stamp_keeps_border(self) -> None:
        """The stamp text was carrying color + weight; WHC strips
        the color so we pin the border + weight."""
        css = _css()
        assert ".deprecation-stamp" in css
        idx = css.index(".deprecation-stamp")
        body = css[idx : idx + 400]
        assert "border: 1px solid CanvasText" in body
        assert "font-weight: 700" in body


class TestStatusBadgeFallback:
    def test_status_badge_gets_border_under_whc(self) -> None:
        css = _css()
        # The forced-colors block contains a .status-badge rule.
        idx_block = css.index("@media (forced-colors: active)")
        block = css[idx_block:]
        assert ".status-badge {" in block
        # Status badge color collapses; replace with CanvasText +
        # a 1px border so the chip shape survives.
        idx = block.index(".status-badge {")
        rule = block[idx : idx + 300]
        assert "color: CanvasText" in rule
        assert "border: 1px solid CanvasText" in rule

    def test_leading_hairline_hidden_under_whc(self) -> None:
        """The ::before hairline relies on currentColor; hide it
        under WHC so the chip's border carries the shape alone."""
        css = _css()
        assert ".status-badge::before" in css
        idx = css.index(".status-badge::before")
        body = css[idx : idx + 200]
        assert "display: none" in body


class TestStatusPillsHighlightToken:
    """Pills (status filter) use color-only fill for checked state.
    Under WHC use system Highlight (the user-chosen selection color)
    so the on/off state is the same the OS uses everywhere else."""

    def test_pill_default_uses_ButtonText(self) -> None:
        css = _css()
        # Grep the whole partial — the .pill rule lives within the
        # forced-colors @media block, after the initial comment + the
        # status-badge block. A windowed search underestimated the
        # offset; the whole-file grep is robust.
        assert ".pill {" in css
        idx2 = css.index(".pill {")
        rule = css[idx2 : idx2 + 300]
        assert "border: 1px solid ButtonText" in rule
        assert "background: Canvas" in rule

    def test_pill_checked_uses_Highlight(self) -> None:
        css = _css()
        assert ".pill:has(input:checked)" in css
        idx = css.index(".pill:has(input:checked)")
        rule = css[idx : idx + 300]
        assert "background: Highlight" in rule
        assert "color: HighlightText" in rule


class TestUntrustedNoticeFallback:
    def test_notice_keeps_left_rule(self) -> None:
        """The .notice element uses a gradient + oxblood left rule.
        Gradient strips under WHC; left rule must use CanvasText."""
        css = _css()
        idx = css.index("@media (forced-colors: active)")
        block = css[idx:]
        assert ".notice {" in block
        idx2 = block.index(".notice {")
        rule = block[idx2 : idx2 + 300]
        assert "border-inline-start: 3px solid CanvasText" in rule


class TestShortcutHintBanner:
    def test_banner_gets_borders_under_whc(self) -> None:
        css = _css()
        assert ".shortcut-hint" in css
        # The banner relies on a background color; pin the border-block
        # fallback so the banner shape is still legible.
        idx = css.index("@media (forced-colors: active)")
        block = css[idx:]
        idx2 = block.index(".shortcut-hint {")
        rule = block[idx2 : idx2 + 300]
        assert "border-block: 1px solid CanvasText" in rule


class TestResultCardHoverFallback:
    def test_hover_hairline_replaced_with_outline(self) -> None:
        """The ::after hover hairline relies on a colour-driven slide
        and currentColor — both die under WHC. Replace with an
        outline that survives forced-colors."""
        css = _css()
        idx = css.index("@media (forced-colors: active)")
        block = css[idx:]
        assert ".result-card::after" in block
        idx2 = block.index(".result-card::after")
        rule = block[idx2 : idx2 + 200]
        assert "display: none" in rule
        # And the static outline replacement.
        assert ".result-card:hover" in block
        idx3 = block.index(".result-card:hover")
        rule3 = block[idx3 : idx3 + 300]
        assert "outline: 2px solid Highlight" in rule3
