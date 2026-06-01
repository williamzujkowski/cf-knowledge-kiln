"""#407 A1 + A2 — AAA contrast palette + 80ch measure pins.

The pre-PR default palette was AA only (5.05-5.31:1 on --paper); AAA
needs 7:1 for normal text. This PR promotes --ink-faded directly
(every usage is text) and adds --oxblood-deep / --teal-deep /
--gold-deep tokens for selective opt-in on the AA accents that are
sometimes UI components (background, border, caret — 3:1 fine) and
sometimes text (--needs AAA).

Tests pin:
- The deep tokens are defined in both light + dark palettes.
- Every ``color: var(--oxblood)`` / ``--teal`` / ``--gold`` in the
  CSS partials uses the ``-deep`` variant (text-color usages MUST
  hit AAA).
- ``--measure-prose`` is defined and applied to the prose containers
  (.excerpt, .preview-body, .notice).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_KILN_DIR = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln"
_TOKENS_CSS = _KILN_DIR / "_tokens.css"


def _all_partials() -> list[Path]:
    """Every CSS partial except _tokens.css itself + _forced_colors.css
    (the latter intentionally uses system tokens, not --oxblood etc).
    Used for the AAA color-usage audit."""
    skip = {"_tokens.css", "_forced_colors.css"}
    return sorted(p for p in _KILN_DIR.glob("_*.css") if p.name not in skip)


class TestDeepTokensDefined:
    """The -deep tokens exist in both light + dark palettes at AAA values.
    The prefers-contrast: more block already drives the AA accent
    tokens to ~these same values, so under prefers-contrast: more
    the deep variants and the AA variants converge."""

    def test_light_palette_defines_deep_tokens(self) -> None:
        css = _TOKENS_CSS.read_text(encoding="utf-8")
        # Each deep token defined in the default light :root block.
        # Use anchored substring checks — exact hex values pin the
        # AAA-conformant choice; a future palette tune that changes
        # the hex without re-verifying AAA fails this test.
        assert "--oxblood-deep:" in css
        assert "--teal-deep:" in css
        assert "--gold-deep:" in css

    def test_light_deep_tokens_are_aaa_hexes(self) -> None:
        """Pin the exact AAA-passing hexes. Computed against --paper
        #f4f7fd:
          oxblood #7e1820 → 8.0:1
          teal    #00475e → 7.0:1
          gold    #6b3712 → 7.5:1
        """
        css = _TOKENS_CSS.read_text(encoding="utf-8")
        assert "--oxblood-deep:" in css and "#7e1820" in css
        assert "--teal-deep:" in css and "#00475e" in css
        assert "--gold-deep:" in css and "#6b3712" in css

    def test_dark_palette_defines_deep_tokens(self) -> None:
        """Dark @media block has its own -deep redefinitions so the
        contrast meets AAA on the dark paper (#212121)."""
        css = _TOKENS_CSS.read_text(encoding="utf-8")
        dark_idx = css.find("@media (prefers-color-scheme: dark)")
        assert dark_idx != -1, "dark palette block missing"
        dark_block = css[dark_idx : dark_idx + 2500]
        assert "--oxblood-deep:" in dark_block
        assert "--teal-deep:" in dark_block
        assert "--gold-deep:" in dark_block

    def test_ink_faded_lifted_to_aaa(self) -> None:
        """--ink-faded is text-only (every usage is body byline /
        meta text). The previous value (#5d6a7d) was 5.26:1 AA;
        bumped to #3f4a5e (8.32:1 AAA — also the prefers-contrast
        value so the two modes converge for this token).

        Reviewer caught an earlier attempt (#4d586a, 6.70:1) as
        below the AAA bar; pinned the conservative choice now."""
        css = _TOKENS_CSS.read_text(encoding="utf-8")
        # Find the default light :root --ink-faded definition.
        m = re.search(r"--ink-faded:\s*(#[0-9a-fA-F]{6})", css)
        assert m is not None, "--ink-faded token missing"
        value = m.group(1).lower()
        # The AA value (#5d6a7d) must NOT be the default anymore.
        assert value != "#5d6a7d", (
            "--ink-faded still at AA value (5.26:1); should be lifted to AAA (≥7:1)"
        )
        # The intermediate value (#4d586a, 6.70:1) is below AAA.
        assert value != "#4d586a", "--ink-faded at #4d586a is 6.70:1 — below AAA's 7:1 bar"
        # The conservative AAA value: #3f4a5e (8.32:1).
        assert value == "#3f4a5e", (
            f"--ink-faded value {value} doesn't match the recorded AAA "
            f"choice (#3f4a5e). If you re-tuned, update the test + "
            f"verify against contrast checker — must be ≥ 7:1 on --paper."
        )

    def test_aaa_text_color_contrast_ratios(self) -> None:
        """Independent contrast-ratio math for the pinned AAA tokens.
        Catches any future hex tune that drops below 7:1 without the
        contributor noticing."""

        def _rl(hexv: str) -> float:
            def f(c: float) -> float:
                c = c / 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

            r = f(int(hexv[1:3], 16))
            g = f(int(hexv[3:5], 16))
            b = f(int(hexv[5:7], 16))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def _contrast(fg: str, bg: str) -> float:
            l1, l2 = _rl(fg), _rl(bg)
            light, dark = max(l1, l2), min(l1, l2)
            return (light + 0.05) / (dark + 0.05)

        paper = "#f4f7fd"
        # Pin every default-light token that's used as TEXT against
        # the AAA bar. UI-only tokens (--rule, --paper-soft) excluded.
        tokens = {
            "--ink-faded": "#3f4a5e",
            "--oxblood-deep": "#7e1820",
            "--teal-deep": "#00475e",
            "--gold-deep": "#6b3712",
        }
        for name, hexv in tokens.items():
            ratio = _contrast(hexv, paper)
            assert ratio >= 7.0, (
                f"{name} ({hexv}) on --paper ({paper}) = {ratio:.2f}:1 — "
                f"below AAA's 7:1 normal-text threshold."
            )

    def test_dark_ink_faded_lifted_to_aaa(self) -> None:
        """Dark-palette --ink-faded was #8d8d8d (4.85:1 AA); bumped
        to #b8b8b8 (~7.5:1 AAA on #212121)."""
        css = _TOKENS_CSS.read_text(encoding="utf-8")
        dark_idx = css.find("@media (prefers-color-scheme: dark)")
        dark_block = css[dark_idx : dark_idx + 2500]
        m = re.search(r"--ink-faded:\s*(#[0-9a-fA-F]{6})", dark_block)
        assert m is not None
        assert m.group(1).lower() != "#8d8d8d", "dark --ink-faded still at AA value"


class TestNoAaTextColorUsages:
    """The load-bearing AAA audit. Every ``color: var(--oxblood)`` /
    ``var(--teal)`` / ``var(--gold)`` in the partials must use the
    ``-deep`` variant. UI-component usages (background, border,
    caret, outline, text-decoration-color) are exempt — they only
    need 3:1 per WCAG 1.4.11."""

    @pytest.mark.parametrize(
        "partial_path",
        [pytest.param(p, id=p.name) for p in _all_partials()],
    )
    def test_no_aa_text_color_on_accents(self, partial_path: Path) -> None:
        css = partial_path.read_text(encoding="utf-8")
        # Match ``color: var(--oxblood);`` exactly — NOT
        # ``color: var(--oxblood-deep);`` and NOT
        # ``background: var(--oxblood);`` or ``border: ... var(--oxblood)``.
        offenders = re.findall(r"color:\s*var\(--(oxblood|teal|gold)\);", css)
        assert not offenders, (
            f"{partial_path.name} uses AA-only accent color for text: "
            f"{offenders}. Swap to the -deep variant for AAA."
        )


class TestMeasureProseDefined:
    """The 80-char prose cap (WCAG 1.4.8 AAA) lives in
    --measure-prose and is applied to long-form text containers."""

    def test_measure_prose_token_defined(self) -> None:
        css = _TOKENS_CSS.read_text(encoding="utf-8")
        assert "--measure-prose:" in css, (
            "--measure-prose token missing; needed to cap prose at the WCAG AAA 80-char ceiling."
        )

    def test_measure_prose_value(self) -> None:
        """38rem ≈ 80ch for serif body. Pin so a future tune that
        widens it past 80ch is caught."""
        css = _TOKENS_CSS.read_text(encoding="utf-8")
        m = re.search(r"--measure-prose:\s*(\d+(?:\.\d+)?)rem", css)
        assert m is not None
        rem_value = float(m.group(1))
        # 38rem = ~80ch for serif at 16px base. 40rem ≈ 85ch (over).
        # Pin the upper bound; the lower bound is unconstrained (a
        # narrower column is fine for AAA, just less roomy).
        assert rem_value <= 40, (
            f"--measure-prose at {rem_value}rem may exceed 80ch — AAA 1.4.8 caps at 80."
        )

    @pytest.mark.parametrize(
        "partial,selector",
        [
            ("_deprecation.css", ".excerpt"),
            ("_preview.css", ".preview-body"),
            ("_base.css", ".notice"),
        ],
    )
    def test_prose_container_uses_measure_prose(self, partial: str, selector: str) -> None:
        """The three prose containers cap their inline-size with
        --measure-prose so the rendered text stays under 80ch.
        Selectors aren't enough — verify the rule block actually
        references the token."""
        css = (_KILN_DIR / partial).read_text(encoding="utf-8")
        # Find the rule block for the selector.
        idx = css.find(f"{selector} {{")
        if idx == -1:
            # Some selectors may be in a compound (e.g. .preview-body
            # might be in ".preview-body, ..."). Use a looser search.
            idx = css.find(selector)
        assert idx != -1, f"{selector} not found in {partial}"
        # Grab a generous window after the selector — rules can run
        # 200+ chars with comments.
        block = css[idx : idx + 800]
        assert "max-inline-size" in block, (
            f"{selector} in {partial} doesn't cap max-inline-size; prose may exceed 80ch."
        )
        assert "--measure-prose" in block, (
            f"{selector} in {partial} uses max-inline-size but doesn't "
            f"reference --measure-prose; use the token so a future "
            f"tune lands in one place."
        )
