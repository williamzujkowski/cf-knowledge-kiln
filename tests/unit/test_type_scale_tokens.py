"""#406 Findings 4 + 5 — modular type scale + leading tokens.

The pre-PR CSS used ~30 ad-hoc font-size values (0.65/0.7/0.72/0.78/
0.82/0.85/0.86/0.88/0.92/0.95/1/1.15/1.25/1.32/1.35/1.4rem plus 16px
body and 8pt/9pt print) with no semantic vocabulary. Body was a hard-
coded 16px, breaking rem-based scaling for users who change their
browser default font size.

This PR introduces the foundation tokens that subsequent migrations
will collapse the ad-hoc cluster into. The token names are size-
agnostic (--text-xs through --text-3xl) so they survive future tunes
of the actual numeric scale.

The body migration alone is load-bearing: switching from 16px to
var(--text-base) at 1rem means a user who sets their browser to 20px
default gets a properly-scaled 1.25x layout across the whole UI
instead of a stuck 16px chrome with everything else floating
relative to a different baseline.

Migration of the existing ad-hoc sizes is OUT OF SCOPE for this
slice — it's a 30+-rule sweep that needs its own PR per cluster so
diffs stay reviewable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TOKENS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln" / "_tokens.css"
_BASE = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln" / "_base.css"


# ─── Type-scale tokens ──────────────────────────────────────────────


# The minor-third (1.2x) scale anchored at --text-base = 1rem.
# Picking discrete rounded values that map cleanly to the existing
# ad-hoc cluster so migrations are obvious:
#   --text-2xs 0.625rem  (collapses 0.65)
#   --text-xs  0.75rem   (collapses 0.7 / 0.72 / 0.78)
#   --text-sm  0.875rem  (collapses 0.82 / 0.85 / 0.86 / 0.88 / 0.9 / 0.92)
#   --text-base 1rem     (replaces 16px body + 0.95 / 1)
#   --text-md  1.125rem  (collapses 1.15)
#   --text-lg  1.25rem   (used directly)
#   --text-xl  1.5rem    (collapses 1.32 / 1.35 / 1.4)
#   --text-2xl 1.875rem  (future-only, no current users)
#   --text-3xl 2.25rem   (future-only, no current users)
_EXPECTED_TEXT_TOKENS: dict[str, str] = {
    "--text-2xs": "0.625rem",
    "--text-xs": "0.75rem",
    "--text-sm": "0.875rem",
    "--text-base": "1rem",
    "--text-md": "1.125rem",
    "--text-lg": "1.25rem",
    "--text-xl": "1.5rem",
    "--text-2xl": "1.875rem",
    "--text-3xl": "2.25rem",
}


@pytest.mark.parametrize(
    "token,expected_value",
    list(_EXPECTED_TEXT_TOKENS.items()),
)
def test_text_token_defined_at_expected_value(token: str, expected_value: str) -> None:
    """Each --text-* token must be defined in the default :root with
    its anchored numeric value. Pinning the value catches:
    - Accidental renumbering that breaks the 1.2x ratio.
    - Drift between this test (which migration PRs check against) and
      the actual token file.

    The values follow a minor-third (1.2x) modular scale anchored at
    --text-base = 1rem. The named steps mirror common design-system
    conventions (Tailwind / Bootstrap / Radix) so a new contributor
    recognizes the vocabulary without a glossary."""
    css = _TOKENS.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(token)}:\s*({re.escape(expected_value)})\s*;",
    )
    assert pattern.search(css) is not None, (
        f"{token} should be {expected_value} in _tokens.css. "
        f"Check :root and ensure no later @media block overrides it "
        f"with a different value at default viewport / contrast / theme."
    )


# ─── Leading tokens ─────────────────────────────────────────────────


_EXPECTED_LEADING_TOKENS: dict[str, str] = {
    # Tight: large display headings where the line-height is purely
    # visual (titles rarely wrap, and when they do the rhythm should
    # feel like a magazine masthead).
    "--leading-tight": "1.15",
    # Snug: subhead / preview titles where 2-3-line wrap is possible
    # and the lines should sit close enough to read as a unit.
    "--leading-snug": "1.3",
    # Body: long-form serif prose. Pinned to 1.6 to match the
    # pre-token visual; the audit's 1.55 recommendation is a
    # deliberate follow-up tune (separate PR — the foundation
    # slice shouldn't introduce a visible reflow).
    "--leading-body": "1.6",
    # Mono: code blocks need extra leading so the descenders and
    # ascenders of a monospaced font don't collide; 1.6 is the
    # community consensus for editor-grade readability.
    "--leading-mono": "1.6",
}


@pytest.mark.parametrize(
    "token,expected_value",
    list(_EXPECTED_LEADING_TOKENS.items()),
)
def test_leading_token_defined_at_expected_value(token: str, expected_value: str) -> None:
    """The --leading-* tokens give a small named vocabulary for
    line-height across the design. Pinning the values keeps a future
    contributor from accidentally diverging the body leading (1.55)
    from the mono leading (1.6) — they're intentionally different,
    not arbitrary."""
    css = _TOKENS.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(token)}:\s*({re.escape(expected_value)})\s*;",
    )
    assert pattern.search(css) is not None, (
        f"{token} should be {expected_value}. Leading tokens are "
        f"unitless so they multiply against the element's font-size."
    )


# ─── Body migration ─────────────────────────────────────────────────


def test_body_font_size_uses_text_base_token() -> None:
    """The pre-PR body rule pinned font-size to ``16px`` — that's a
    pixel value that ignores the user's browser default. Switching to
    ``var(--text-base)`` (= 1rem = the user's preferred default)
    means the entire UI scales correctly when a low-vision user
    bumps their browser font size from 16 to 18 or 20.

    Pin the rule directly in _base.css so a future ``font-size: Npx``
    regression is caught — pixels are the wrong unit for body type."""
    css = _BASE.read_text(encoding="utf-8")
    # Find the top-level body rule (NOT inside an @media block).
    m = re.search(r"^body\s*\{([^}]+)\}", css, re.MULTILINE)
    assert m is not None, "Top-level body rule missing in _base.css"
    block = m.group(1)
    assert "font-size: var(--text-base)" in block, (
        "body rule should set font-size: var(--text-base) so the UI "
        "scales with the user's browser font-size preference. The "
        "old `font-size: 16px` defeats that."
    )
    # Defence-in-depth: no leftover px font-size in the body rule.
    assert "font-size: 16px" not in block, (
        "body rule still has font-size: 16px — was the migration left half-done?"
    )


def test_body_line_height_uses_leading_body_token() -> None:
    """Similar story for line-height: the body's 1.6 is a magic
    number. Migrate to var(--leading-body) so a future tune of
    long-form-prose leading lands in one place."""
    css = _BASE.read_text(encoding="utf-8")
    m = re.search(r"^body\s*\{([^}]+)\}", css, re.MULTILINE)
    assert m is not None
    block = m.group(1)
    assert "line-height: var(--leading-body)" in block, (
        "body rule should set line-height: var(--leading-body) so "
        "the prose-leading token is the single source of truth."
    )


# ─── Migration sweeps ───────────────────────────────────────────────


_KILN_DIR = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln"


def test_no_orphan_0_78rem_font_size() -> None:
    """First migration sweep (#406): the 0.78rem cluster collapses
    to var(--text-xs). After the sweep, no partial should still
    declare a literal ``font-size: 0.78rem`` — every one moved to
    the token. A grep across all partials catches the regression
    where a new rule sneaks in with the old ad-hoc value."""
    offenders: list[str] = []
    for partial in sorted(_KILN_DIR.glob("_*.css")):
        css = partial.read_text(encoding="utf-8")
        if "font-size: 0.78rem" in css:
            offenders.append(partial.name)
    assert not offenders, (
        f"font-size: 0.78rem should have migrated to var(--text-xs) "
        f"after #406 sweep 1. Still present in: {offenders}. The "
        f"token collapses 0.7 / 0.72 / 0.78 into one value — adding "
        f"a new 0.78rem rule defeats the point."
    )
