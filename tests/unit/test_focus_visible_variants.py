"""Pins the #356 fix: focus-visible has prefers-contrast + forced-colors variants.

Audit Finding #21: the 2px oxblood ring works for typical users
but degrades for:
* Low-vision users (need thicker ring at prefers-contrast: more)
* Windows High Contrast (forced-colors strips author colours; needs
  the system Highlight token to draw a deliberate ring)

These tests grep _base.css for the variant blocks so a future
refactor that drops one is caught.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_BASE_CSS = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_base.css"


def _css() -> str:
    return _BASE_CSS.read_text()


class TestPrefersContrastVariant:
    def test_block_present(self) -> None:
        css = _css()
        assert "@media (prefers-contrast: more)" in css

    def test_widens_outline_to_3px(self) -> None:
        """Low-vision users get a thicker ring + larger offset.
        Don't change the colour — oxblood is already AA contrast."""
        css = _css()
        idx = css.index("@media (prefers-contrast: more)")
        block = css[idx : idx + 500]
        # focus-visible variants must scope to a/button/input.
        assert "a:focus-visible" in block
        assert "outline-width: 3px" in block
        assert "outline-offset: 3px" in block


class TestForcedColorsVariant:
    def test_block_present(self) -> None:
        css = _css()
        assert "@media (forced-colors: active)" in css

    def test_uses_system_highlight_token(self) -> None:
        """WHC strips author colours; the focus ring must use the
        system Highlight token so it matches the user's chosen
        palette."""
        css = _css()
        # Find the forced-colors block within the focus-visible
        # variants. Widened to 900 chars because the comment block
        # inside the rule pushes the actual declarations down.
        idx = css.index("@media (forced-colors: active)")
        block = css[idx : idx + 900]
        assert "outline: 3px solid Highlight" in block
        assert "forced-color-adjust: none" in block

    def test_outline_offset_preserved(self) -> None:
        """Even with author colour stripped, the offset stays so
        the ring sits OUTSIDE the focused element, not on top."""
        css = _css()
        idx = css.index("@media (forced-colors: active)")
        block = css[idx : idx + 900]
        assert "outline-offset: 2px" in block
