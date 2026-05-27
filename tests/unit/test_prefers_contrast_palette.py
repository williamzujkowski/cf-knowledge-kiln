"""Pins the #351 prefers-contrast palette.

Audit Finding #19 / Epic C #328. Low-vision users with
prefers-contrast: more set need AAA contrast ratios; the default
palette only reaches AA. This palette swap deepens the inks +
darkens the accents so paired ratios pass 7:1 against paper.

Tests pin the @media block + the specific token overrides; an
automatic-contrast verification step is out of scope (would need
to parse hex + compute luminance) but can be added as a follow-up.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_TOKENS = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_tokens.css"


def _css() -> str:
    return _TOKENS.read_text()


class TestPrefersContrastBlock:
    def test_media_query_present(self) -> None:
        assert "@media (prefers-contrast: more)" in _css()

    def test_block_overrides_ink_tokens(self) -> None:
        css = _css()
        idx = css.index("@media (prefers-contrast: more)")
        # Look at next ~1500 chars (the block).
        block = css[idx : idx + 1500]
        # Every foreground token must be overridden — the audit's
        # rationale was that the inks plateau at AA. Pin each.
        for token in ("--ink:", "--ink-soft:", "--ink-faded:"):
            assert token in block, (
                f"@media (prefers-contrast: more) must override {token}; "
                f"missing means the base palette wins for high-contrast users."
            )

    def test_block_overrides_accent_tokens(self) -> None:
        css = _css()
        idx = css.index("@media (prefers-contrast: more)")
        block = css[idx : idx + 1500]
        for token in ("--oxblood:", "--teal:", "--gold:"):
            assert token in block, f"missing accent override: {token}"

    def test_block_bumps_oxblood_stripe_alpha(self) -> None:
        """The deprecation hatch needs a higher alpha against the
        darker inks of high-contrast mode so the stripe doesn't
        visually disappear."""
        css = _css()
        idx = css.index("@media (prefers-contrast: more)")
        block = css[idx : idx + 1500]
        assert "--oxblood-stripe:" in block


class TestCombinedDarkHighContrast:
    """A user with both prefers-color-scheme: dark AND
    prefers-contrast: more set needs a third palette — without it
    the dark-mode tokens (declared after the prefers-contrast block)
    win and override the high-contrast tokens."""

    def test_combined_media_query_present(self) -> None:
        css = _css()
        assert "@media (prefers-contrast: more) and (prefers-color-scheme: dark)" in css

    def test_combined_block_carries_overrides(self) -> None:
        css = _css()
        idx = css.index("@media (prefers-contrast: more) and (prefers-color-scheme: dark)")
        block = css[idx : idx + 1500]
        # Sanity: re-overrides the same tokens.
        for token in ("--ink:", "--oxblood:", "--teal:", "--gold:"):
            assert token in block, f"combined block missing: {token}"


class TestBundleCarriesPalette:
    """Pin that make build-css folded the new tokens into kiln.css."""

    def test_bundle_includes_prefers_contrast(self) -> None:
        bundle = (_REPO / "src/cf_knowledge_kiln/api/static/kiln.css").read_text()
        assert "@media (prefers-contrast: more)" in bundle
