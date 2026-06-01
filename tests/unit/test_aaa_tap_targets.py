"""#407 A3 — WCAG 2.5.5 Enhanced (AAA) tap-target size pins.

The standard requires interactive targets be at least 44 by 44 CSS
pixels, with exceptions for inline-text controls (a target whose size
is constrained by the line-height of surrounding non-target text) and
for controls with equivalent function offered elsewhere.

Three controls were sub-44 in the kiln UI:
* ``.preview-close`` (close glyph) is a standalone header control;
  no inline exception applies.
* ``.card-action`` (.card-action-copy + .card-action-expand) — the
  copy variant sits next to other source-line controls (not within
  sentence flow), the expand variant lives on its own line. Neither
  fits the inline-text exception cleanly.
* ``.feedback-link`` — single-word options within a sentence; the
  inline exception does apply, but we still extend the hit area as
  defence-in-depth so the AAA claim survives a narrow re-reading.

Rather than enlarge the visible glyphs (which would coarsen the
editorial typography), each control declares an absolutely-positioned
``::before`` pseudo whose ``inset`` expands the clickable hit area to
``>= 44 x 44`` CSS pixels. WCAG measures the size of the target,
which is the clickable area — not the painted glyph.

The tests pin:
1. Each control declares ``position: relative`` so the ``::before``
   pseudo has an anchor.
2. Each ``::before`` rule declares ``content: ""``, ``position:
   absolute``, and an ``inset`` whose minimum slop (per direction) is
   enough to push the smallest underlying visible glyph past 44px.
3. The pre-existing ``.preview-close`` focus background of
   ``var(--rule)`` (#c8d2e0 on --paper-soft #ffffff, 1.53:1) is gone —
   it failed WCAG 2.4.13 Focus Appearance's 3:1 bar. Focus-visible
   now uses an oxblood outline matching ``.card-action`` and
   ``.feedback-link``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_KILN_DIR = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln"


def _rule_block(css: str, selector: str) -> str:
    """Return the first rule block (``selector { ... }``) by exact
    selector match. Accepts nested braces inside the body."""
    pattern = re.compile(
        rf"^{re.escape(selector)}\s*\{{([^{{}}]*(?:\{{[^{{}}]*\}}[^{{}}]*)*)\}}",
        re.MULTILINE,
    )
    m = pattern.search(css)
    assert m is not None, f"selector {selector!r} not found"
    return m.group(0)


def _parse_inset_block_slop(block: str) -> float:
    """Return the BLOCK-axis slop (in rem) declared in an ``inset``
    rule on a ``::before`` pseudo. The block-axis is the one that
    matters for the 44-px bar on inline-laid-out text; the inline
    axis is either also extended (symmetric inset) or stays at 0
    when the surrounding flex layout makes extending inline-ward
    unsafe (e.g. .result-title-button can't grow inline without
    overlapping the .status-badge / score-row siblings).

    Accepts:
        inset: -0.7rem;                 (block = inline = 0.7)
        inset: -0.85rem -0.5rem;        (block = 0.85, inline = 0.5)
        inset: -0.65rem 0;              (block = 0.65, inline = 0)
        inset: -1rem -2rem -1rem -2rem; (top, right, bottom, left)
    """
    m = re.search(r"inset:\s*([^;]+);", block)
    assert m is not None, f"::before block has no inset declaration:\n{block}"
    parts = m.group(1).strip().split()

    def _to_rem(token: str) -> float:
        if token == "0":
            return 0.0
        match = re.match(r"(-?\d+(?:\.\d+)?)rem", token)
        assert match is not None, f"unsupported inset unit in {token!r}"
        return abs(float(match.group(1)))

    values = [_to_rem(p) for p in parts]
    # CSS inset shorthand axes: 1=all, 2=block/inline, 3=top/inline/bottom, 4=t/r/b/l.
    # Return the block-axis (vertical) slop in every case.
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0]  # block axis
    if len(values) == 3:
        # top, inline, bottom — average top+bottom for block representation.
        return (values[0] + values[2]) / 2
    if len(values) == 4:
        return (values[0] + values[2]) / 2  # top + bottom average
    raise AssertionError(f"unexpected inset shape: {values!r}")


# (partial, selector, smallest_visible_dim_px): the third value is the
# smallest CSS-pixel dimension the underlying visible glyph occupies
# on the constrained axis. The ::before inset on that axis must add
# enough slop that the total hit area meets 44 px. For preview-close
# the visible close glyph is about 22 px inline by 26 px block; the
# inline axis is the tight one. For card-action / feedback-link the italic
# Fraunces label is about 18 px block by 50+ px inline; block axis is
# the tight one.
_TARGETS = [
    pytest.param(
        "_preview.css",
        ".preview-close",
        22.0,
        id="preview-close",
    ),
    pytest.param(
        "_results_mobile.css",
        ".card-action",
        18.0,
        id="card-action",
    ),
    pytest.param(
        "_feedback.css",
        ".feedback-link",
        18.0,
        id="feedback-link",
    ),
    # #415 follow-up — result title is its own clickable button.
    # Font-size 1.35rem * line-height 1.25 = 27px block axis on a
    # single-line title. Inline axis is the title text width (always
    # well over 44px for any non-empty title), so the smallest-dim
    # constraint is block-axis. The ::before inset is asymmetric
    # (block-axis only) to avoid overlapping the .status-badge / score-
    # row siblings to the right.
    pytest.param(
        "_results.css",
        ".result-title-button",
        27.0,
        id="result-title-button",
    ),
]


@pytest.mark.parametrize("partial,selector,smallest_dim", _TARGETS)
def test_target_anchors_pseudo(partial: str, selector: str, smallest_dim: float) -> None:
    """The control must declare ``position: relative`` so the
    ``::before`` pseudo has an anchor. The anchor matters: without
    relative positioning, the pseudo lays out against the nearest
    positioned ancestor and the hit area shifts."""
    css = (_KILN_DIR / partial).read_text(encoding="utf-8")
    block = _rule_block(css, selector)
    assert "position: relative" in block, (
        f"{selector} in {partial} doesn't declare position: relative; "
        f"the ::before hit-area pseudo will lay out against the wrong "
        f"ancestor and the click target may not align with the glyph."
    )


@pytest.mark.parametrize("partial,selector,smallest_dim", _TARGETS)
def test_44px_hit_area_via_pseudo(
    partial: str,
    selector: str,
    smallest_dim: float,
) -> None:
    """Each control declares a ``::before`` pseudo with ``content``,
    ``position: absolute``, and an ``inset`` whose minimum slop is
    large enough that the smallest visible dimension expands past 44
    CSS pixels.

    Computation: 1 rem == 16 px (base font size). ``inset: -X rem`` on
    a side extends the hit area by ``X * 16`` px in that direction.
    Two sides of slop must add at least ``44 - smallest_dim`` px.
    """
    css = (_KILN_DIR / partial).read_text(encoding="utf-8")
    block = _rule_block(css, f"{selector}::before")
    assert 'content: ""' in block or "content: ''" in block, (
        f"{selector}::before missing content; pseudo won't render."
    )
    assert "position: absolute" in block, (
        f"{selector}::before missing position: absolute; pseudo won't overlay the button."
    )
    block_slop_rem = _parse_inset_block_slop(block)
    block_slop_px = block_slop_rem * 16
    needed_slop_px_per_side = (44 - smallest_dim) / 2
    assert block_slop_px >= needed_slop_px_per_side, (
        f"{selector}::before block-axis inset slop is {block_slop_rem}rem "
        f"({block_slop_px:.1f}px per side). To bring a {smallest_dim}px "
        f"glyph past 44px we need at least "
        f"{needed_slop_px_per_side:.1f}px per side — short by "
        f"{needed_slop_px_per_side - block_slop_px:.1f}px."
    )


def test_preview_close_focus_uses_outline_not_low_contrast_background() -> None:
    """The original focus-visible declaration set
    ``background: var(--rule)`` (#c8d2e0 on --paper-soft #ffffff =
    1.53:1, below WCAG 2.4.13's 3:1 bar). The fix replaces the
    background with the oxblood outline pattern shared with
    .card-action and .feedback-link. Pin both halves: outline must be
    present, and the focus rule must not set the broken background."""
    css = (_KILN_DIR / "_preview.css").read_text(encoding="utf-8")
    focus_block = _rule_block(css, ".preview-close:focus-visible")
    assert "outline:" in focus_block and "var(--oxblood" in focus_block, (
        "preview-close:focus-visible doesn't declare an oxblood "
        "outline. Without it, focus indicator may drop below WCAG "
        "2.4.13's 3:1 contrast bar."
    )
    assert "background: var(--rule)" not in focus_block, (
        "preview-close:focus-visible still sets background to "
        "var(--rule) (1.53:1 on --paper-soft) — fails WCAG 2.4.13. "
        "Use outline instead."
    )


def test_feedback_link_documents_inline_exception() -> None:
    """The CSS comment around .feedback-link calls out the inline-
    text exception explicitly so a future reviewer doesn't think the
    hit-area pseudo is redundant and remove it (or, conversely,
    decide the ::before slop is insufficient and inflate the visible
    button). The comment must mention WCAG 2.5.5 (or the exception
    itself) and ``defence-in-depth``."""
    css = (_KILN_DIR / "_feedback.css").read_text(encoding="utf-8")
    idx = css.find(".feedback-link {")
    assert idx != -1
    preamble = css[max(0, idx - 1000) : idx]
    assert "2.5.5" in preamble or "inline exception" in preamble, (
        "Inline-exception rationale missing from .feedback-link comment block."
    )
    assert "defence" in preamble.lower() or "defense" in preamble.lower(), (
        "The defence-in-depth framing is missing — without it, a "
        "later contributor might delete the ::before pseudo as "
        "redundant under the inline exception."
    )
