"""Pins the #343 fix: masthead compresses on narrow viewports.

Audit Finding #26: on 360px viewports the masthead consumed ~180px
above the search input — the dominant interaction sat below the
fold on first paint. This PR adds @media rules at 640px and 480px
that tighten vertical rhythm so the search input lands above-fold
on iPhone Mini.

Tests grep _base.css for the breakpoints and the specific
declarations that drive the compression.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_BASE_CSS = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_base.css"


def _css() -> str:
    return _BASE_CSS.read_text()


class TestMobileMastheadCompression:
    def test_640px_breakpoint_present(self) -> None:
        css = _css()
        # The 640px block must mention .masthead so the compression
        # is scoped.
        assert "@media (max-width: 640px)" in css
        # Quick sanity that the 640px block contains a masthead rule.
        idx = css.index("@media (max-width: 640px)")
        block = css[idx : idx + 800]
        assert ".masthead" in block

    def test_480px_breakpoint_present(self) -> None:
        css = _css()
        assert "@media (max-width: 480px)" in css

    def test_shortcut_hint_hidden_below_480px(self) -> None:
        """Keyboard shortcut mnemonics aren't useful on a touch
        keyboard; below 480px the hint banner is display:none so
        it stops eating above-fold space."""
        css = _css()
        # Find the 480px block.
        idx = css.index("@media (max-width: 480px)")
        block = css[idx : idx + 800]
        # The shortcut-hint rule must be inside this media query
        # (NOT in the desktop default).
        assert ".shortcut-hint" in block
        assert "display: none" in block

    def test_masthead_padding_reduces_at_640px(self) -> None:
        """Masthead top/bottom padding must shrink so the search
        input lands above-fold. The 640px block sets a smaller
        padding than the desktop default (which uses 2.5rem 1.5rem)."""
        css = _css()
        idx = css.index("@media (max-width: 640px)")
        block = css[idx : idx + 800]
        # The padding-block rule inside the masthead selector.
        assert ".masthead" in block
        assert "padding-block: 1.25rem" in block or "padding-block:1.25rem" in block

    def test_brand_rule_hidden_below_480(self) -> None:
        """Below 480px the .brand-rule visual chrome (the hairline
        between mark and name) is removed so the brand reads as a
        single inline-flex pair without an awkward separator."""
        css = _css()
        idx = css.index("@media (max-width: 480px)")
        block = css[idx : idx + 800]
        assert ".brand-rule" in block
        assert "display: none" in block
