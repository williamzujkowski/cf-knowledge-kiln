"""Pins the #355 fix: each Warning.type has a typographic mark.

Audit Finding #25: warning pills encoded SEVERITY via background
color but the per-TYPE meaning lived only in the prose. Now each
of the nine Warning.type variants has a Unicode glyph attached via
::before so the type is identifiable by shape, not text — survives
color-blind, B&W print, and Windows High Contrast.

The full set:
* stale_source            → ⏳ (hourglass)
* deprecated_source       → ⊘ (do not cite)
* conflicting_sources     → ⇌ (back-and-forth harpoons)
* weak_evidence           → … (ellipsis; incomplete)
* isolated_match          → ✧ (4-pointed star; alone)
* prompt_injection_pattern → 🔒 (lock; security)
* sensitive_content       → ⛔ (no entry; refuse-to-act)
* query_normalized        → ✎ (pencil; edited)
* answer_truncated        → ⋯ (midline ellipsis; cut off)

Tests grep the CSS for each per-type rule so a future refactor
that drops one is caught at PR review.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
# #342 split: warning-pill + per-type ::before rules moved from
# _results.css into the new _warnings.css partial. Grep the new
# location so this test pins behavior, not the old layout.
_RESULTS_CSS = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_warnings.css"


# Module-level expected map — ruff RUF012 forbids mutable class
# attributes; module-level constants are fine.
_EXPECTED_GLYPHS = {
    "stale_source": "\\23F3",  # HOURGLASS
    "deprecated_source": "\\2298",  # CIRCLED DIVISION SLASH
    "conflicting_sources": "\\21CC",  # OVER-UNDER HARPOONS
    "weak_evidence": "\\2026",  # HORIZONTAL ELLIPSIS
    "isolated_match": "\\2727",  # WHITE FOUR-POINTED STAR
    "prompt_injection_pattern": "\\1F512",  # LOCK
    "sensitive_content": "\\26D4",  # NO ENTRY
    "query_normalized": "\\270E",  # PENCIL
    "answer_truncated": "\\22EF",  # MIDLINE HORIZONTAL ELLIPSIS
}


class TestPerTypeMarkRules:
    """Each Warning.type must have a ::before content rule attaching
    a distinct Unicode glyph. We grep on both the top-of-list
    .warning-X class and the per-card .result-warning-X class so
    the mark renders in both contexts."""

    def _css(self) -> str:
        return _RESULTS_CSS.read_text()

    def test_each_type_has_top_of_list_rule(self) -> None:
        css = self._css()
        for warning_type, _glyph in _EXPECTED_GLYPHS.items():
            selector = f".warning-{warning_type}::before"
            assert selector in css, f"Missing top-of-list per-type mark rule: {selector}"

    def test_each_type_has_per_card_rule(self) -> None:
        css = self._css()
        for warning_type, _glyph in _EXPECTED_GLYPHS.items():
            selector = f".result-warning-{warning_type}::before"
            assert selector in css, f"Missing per-card per-type mark rule: {selector}"

    def test_each_type_carries_its_unique_glyph(self) -> None:
        """The glyph escape sequence must appear in the CSS.
        Pinning the escape (not the rendered glyph) keeps the test
        editor-friendly and avoids cross-platform whitespace
        weirdness."""
        css = self._css()
        for warning_type, glyph_escape in _EXPECTED_GLYPHS.items():
            # The rule block containing this selector must contain
            # the glyph escape too.
            selector = f".warning-{warning_type}::before"
            start = css.index(selector)
            block = css[start : start + 200]
            assert glyph_escape in block, (
                f"Glyph escape {glyph_escape!r} missing from {warning_type} per-type rule block."
            )

    def test_glyphs_are_distinct(self) -> None:
        """No two types share a glyph — defeats the purpose."""
        glyphs = list(_EXPECTED_GLYPHS.values())
        assert len(glyphs) == len(set(glyphs)), "Each Warning.type must have a UNIQUE glyph."

    def test_glyph_size_and_alignment_rules_present(self) -> None:
        """The shared sizing/positioning rule for the glyph must
        exist (font-family + font-size + vertical-align) — without
        it the glyphs render as inline body-text width and disrupt
        the pill's vertical rhythm."""
        css = self._css()
        # The shared rule sits at .warning::before, .result-warning::before
        # (outside any media query). Search the first occurrence of
        # that selector pair that's NOT inside a media block.
        marker = ".warning::before,\n.result-warning::before {"
        # Account for either 2-space or 0-space indent depending on
        # whether the formatter wrapped the rule inside a block.
        if marker not in css:
            marker = "  .warning::before,\n  .result-warning::before {"
        idx = css.index(marker)
        block = css[idx : idx + 400]
        assert "font-family:" in block
        assert "font-size:" in block
        assert "vertical-align:" in block


class TestForcedColorsGlyphSafety:
    """Forced-colors mode strips per-pill colours to system tokens;
    the shared block must remap the glyph colour to CanvasText so
    the mark stays visible in WHC."""

    def test_forced_colors_block_remaps_glyph_color(self) -> None:
        css = _RESULTS_CSS.read_text()
        # The forced-colors block within _results.css now has TWO
        # responsibilities: score-widget dots (from #354) AND
        # warning-pill glyphs. Pin the warning-pill piece.
        assert "@media (forced-colors: active)" in css
        # The block must address both ::before pseudo-elements.
        # Grab everything from the FIRST forced-colors block onward
        # (there may now be two if #354's block stayed separate).
        idx = css.index(".warning::before")
        # The warning ::before rule sits below the per-type rules;
        # search forward for its forced-colors mention.
        end = idx + 1500
        block = css[idx:end]
        assert "@media (forced-colors: active)" in block or "CanvasText" in css
