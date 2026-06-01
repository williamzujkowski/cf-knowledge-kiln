"""#407 A3 — WCAG 2.5.5 Enhanced (AAA) tap-target size pins.

The standard requires interactive targets be at least 44 by 44 CSS
pixels, with exceptions for inline-text controls (a target whose size
is constrained by the line-height of surrounding non-target text) and
for controls with equivalent function offered elsewhere.

In the kiln UI three controls were sub-44:
* ``.preview-close`` (close glyph, ~22x26) is a standalone header
  control; no inline exception applies. MUST hit 44x44.
* ``.card-action-expand`` sits on its own line between the excerpt
  and the footer. Not within a sentence. MUST hit 44x44.
* ``.card-action-copy`` and ``.feedback-link`` are single-word
  options within sentences (".. did this answer .." / source-line).
  Inline exception applies; size unchanged. The exception is
  documented in the CSS comment so a future reviewer doesn't "fix"
  a non-violation.

The tests pin the two affirmative cases and confirm the two exempt
cases carry the documenting comment so the rationale survives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_KILN_DIR = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln"


def _selector_block(css: str, selector: str) -> str:
    """Return the first rule block (``selector { ... }``) by exact
    selector match. The matcher is greedy enough to span comments
    inside the block but stops at the first unbalanced ``}``."""
    pattern = re.compile(
        rf"^{re.escape(selector)}\s*\{{([^{{}}]*(?:\{{[^{{}}]*\}}[^{{}}]*)*)\}}",
        re.MULTILINE,
    )
    m = pattern.search(css)
    assert m is not None, f"selector {selector!r} not found"
    return m.group(0)


@pytest.mark.parametrize(
    "partial,selector",
    [
        ("_preview.css", ".preview-close"),
        ("_results_mobile.css", ".card-action-expand"),
    ],
)
def test_44px_tap_target(partial: str, selector: str) -> None:
    """The two controls that don't get the inline exception each
    declare ``min-block-size: 44px`` AND ``min-inline-size: 44px``
    so they meet WCAG 2.5.5 Enhanced from any direction."""
    css = (_KILN_DIR / partial).read_text(encoding="utf-8")
    block = _selector_block(css, selector)
    assert "min-block-size: 44px" in block, (
        f"{selector} in {partial} missing min-block-size: 44px — "
        f"WCAG 2.5.5 Enhanced requires ≥44 CSS px in the block axis."
    )
    assert "min-inline-size: 44px" in block, (
        f"{selector} in {partial} missing min-inline-size: 44px — "
        f"WCAG 2.5.5 Enhanced requires ≥44 CSS px in the inline axis."
    )


@pytest.mark.parametrize(
    "partial,selector",
    [
        ("_feedback.css", ".feedback-link"),
        ("_results_mobile.css", ".card-action"),
    ],
)
def test_inline_exception_documented(partial: str, selector: str) -> None:
    """The two controls that DO get the WCAG 2.5.5 inline exception
    must say so in a comment immediately preceding the rule, so a
    future contributor doing a WCAG sweep doesn't 'fix' a
    non-violation and disturb the inline baseline."""
    css = (_KILN_DIR / partial).read_text(encoding="utf-8")
    idx = css.find(f"{selector} {{")
    assert idx != -1, f"{selector} not found in {partial}"
    # Walk back 800 chars and look for the documenting marker. The
    # comment must mention either WCAG 2.5.5, the inline exception,
    # or A3 — any of those signals intent.
    preamble = css[max(0, idx - 800) : idx]
    markers = ("2.5.5", "inline exception", "#407 A3")
    assert any(m in preamble for m in markers), (
        f"{selector} in {partial} doesn't carry an inline-exception "
        f"comment within 800 chars before the rule. Required so a "
        f"reviewer can see the size is intentional, not a miss."
    )
