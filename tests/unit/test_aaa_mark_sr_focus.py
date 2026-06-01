"""#407 A4 + A5 — mark SR boundary + search-input focus appearance.

**A4 (WCAG 1.3.1)** — bare ``<mark>`` is not announced by most
screen readers; AT users get no programmatic signal that the wrapped
text is the query-match highlight. Fix: wrap mark contents with
visually-hidden bracket characters so SR reads
``[ highlighted text ]`` and a boundary is audible without spamming
"match" twenty times per excerpt. Pairs with a ``.visually-hidden``
utility class so the brackets are removed from the visual layout.

**A5 (WCAG 2.4.13 Focus Appearance)** — the search input itself sets
``outline: 0`` on focus-visible; the visual focus indicator is a
2 CSS-pixel solid ``border-block-end`` on the parent ``.query-row``
that shifts from ``var(--rule)`` to ``var(--oxblood-deep)`` via
``:focus-within``. Verify the contrast meets the 3:1 floor in both
adjacent-color and against-prior-indicator directions, and that the
2px thickness clears the 2.4.13 minimum-area bar. The test pins the
selectors, thickness, and token so a future contributor can't
silently undo the AAA-compliant choice (e.g. by changing to
``var(--oxblood)`` AA or dropping the thickness to 1px).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cf_knowledge_kiln.api.result_cards import highlight_excerpt

_REPO = Path(__file__).resolve().parents[2]
_SEARCH_CSS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln" / "_search.css"
_UTILITIES_CSS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln" / "_utilities.css"


# ─── A4 — mark SR boundary ──────────────────────────────────────────


def test_highlight_excerpt_wraps_marks_with_visually_hidden_brackets() -> None:
    """Each ``<mark>`` carries visually-hidden bracket sentinels so SR
    users hear an audible boundary. The brackets are pronounced as
    "left/right square bracket" by NVDA / VoiceOver / JAWS without
    requiring screen-reader-specific markup."""
    result = str(highlight_excerpt("hello world", "world"))
    # The wrapping pattern is:
    #   <mark><span class="visually-hidden">[</span>WORD<span class="visually-hidden">]</span></mark>
    assert '<mark><span class="visually-hidden">[' in result, (
        f"Open-bracket sentinel missing inside <mark>. Got: {result!r}"
    )
    assert '<span class="visually-hidden">]</span></mark>' in result, (
        f"Close-bracket sentinel missing inside <mark>. Got: {result!r}"
    )


def test_highlight_excerpt_brackets_only_inside_mark() -> None:
    """The bracket sentinels must live ONLY inside <mark> tags, not
    leak into surrounding excerpt prose."""
    result = str(highlight_excerpt("hello world", "world"))
    # Strip mark blocks and confirm no orphan visually-hidden spans.
    stripped = re.sub(r"<mark>.*?</mark>", "", result, flags=re.DOTALL)
    assert "visually-hidden" not in stripped, (
        f"visually-hidden span leaked outside <mark>. Stripped excerpt: {stripped!r}"
    )


def test_highlight_excerpt_no_query_returns_unwrapped() -> None:
    """Sanity: empty query returns the plain escaped excerpt with
    no visually-hidden sentinels (no marks were inserted)."""
    result = str(highlight_excerpt("hello world", ""))
    assert "visually-hidden" not in result
    assert "<mark>" not in result


def test_highlight_excerpt_xss_safety_preserved() -> None:
    """Defence-in-depth: the new mark wrapper must not bypass the
    HTML-escaping pass on the source excerpt. A script tag in the
    excerpt should still come out escaped."""
    result = str(highlight_excerpt("<script>alert(1)</script> world", "world"))
    assert "<script>" not in result, f"Excerpt-side <script> leaked through. Got: {result!r}"
    assert "&lt;script&gt;" in result, f"Expected escaped script tag. Got: {result!r}"


def test_visually_hidden_class_defined() -> None:
    """The utility class needs to render the bracket sentinels off-
    screen without removing them from the accessibility tree (so SR
    still announces them). Pin the canonical clipped-1px pattern."""
    css = _UTILITIES_CSS.read_text(encoding="utf-8")
    assert ".visually-hidden" in css, (
        "Utility class .visually-hidden missing from _utilities.css — "
        "the <mark> bracket sentinels would render visually, defeating "
        "the editorial typography."
    )
    # Pin three load-bearing declarations so a future contributor
    # who simplifies to `display: none` (which removes from a11y tree)
    # gets caught.
    idx = css.find(".visually-hidden")
    block = css[idx : idx + 600]
    assert "position: absolute" in block, "visually-hidden needs position: absolute"
    assert "clip-path: inset(50%)" in block or "clip:" in block, (
        "visually-hidden needs a clip rule so the span is not in the visual layout"
    )
    assert "display: none" not in block, (
        "visually-hidden must NOT use display: none — that removes the "
        "span from the accessibility tree too."
    )


# ─── A5 — search input focus appearance ─────────────────────────────


def test_query_input_focus_relies_on_query_row_indicator() -> None:
    """``.query-input:focus-visible`` declares ``outline: 0`` — the
    visible focus indicator is the parent ``.query-row``'s 2px
    border-block-end shift. Pin the rule + the explanatory comment so
    a future contributor doesn't add ``outline: 0`` somewhere else
    without an indicator, AND doesn't remove the existing one without
    realizing the .query-row carries the AAA-compliant signal."""
    css = _SEARCH_CSS.read_text(encoding="utf-8")
    # query-input:focus-visible block
    m = re.search(
        r"\.query-input:focus-visible\s*\{[^}]*\}",
        css,
    )
    assert m is not None, ".query-input:focus-visible rule missing"
    block = m.group(0)
    assert "outline: 0" in block, (
        "query-input:focus-visible should declare outline: 0 — the parent "
        ".query-row carries the visible indicator."
    )


@pytest.mark.parametrize(
    "selector",
    [
        ".query-row:focus-within",
        ".rail-field-input:focus-visible",
    ],
)
def test_focus_indicator_uses_aaa_token_at_2px(selector: str) -> None:
    """The two focus indicators in the search column share the same
    pattern: border-block-end shifts to ``var(--oxblood-deep)`` at
    2px. Pin both:
    - The token MUST be the AAA-deep variant (--oxblood at AA
      #a52430 = 5.31:1 vs --paper, which would still meet 2.4.13's
      3:1 floor but only by ~2x margin; --oxblood-deep #7e1820 =
      9.60:1 vs --paper, 6.75:1 vs --rule, the comfortable AAA
      bracket).
    - The thickness MUST be 2px — 2.4.13 requires either a 2
      CSS-pixel perimeter or a 2 CSS-pixel line along a long side.
    Both .query-row:focus-within and .rail-field-input:focus-visible
    use the same pattern; the test covers them together so a future
    refactor that only updates one selector is caught."""
    css = _SEARCH_CSS.read_text(encoding="utf-8")
    pattern = re.escape(selector) + r"\s*\{[^}]*\}"
    m = re.search(pattern, css)
    assert m is not None, f"{selector} rule missing"
    block = m.group(0)
    assert "var(--oxblood-deep)" in block, (
        f"{selector} border color should use --oxblood-deep "
        f"(AAA, 9.60:1 on --paper); switching to --oxblood (AA, "
        f"5.31:1) would drop contrast against adjacent surfaces."
    )
    assert "2px" in block, (
        f"{selector} border thickness should be 2px to clear "
        f"WCAG 2.4.13's minimum-area requirement for a single-side "
        f"indicator."
    )
