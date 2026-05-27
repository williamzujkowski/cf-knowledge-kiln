"""Pins the #354 fix: score widget conveys tier via THREE channels.

Audit Finding #24: tier 5 vs tier 2 was hard to distinguish for
deutan/protan users at the small dot size because color was the
only differentiator. The widget now layers three:

1. Color (per-tier, the original cue — preserved)
2. Numeric prefix "N/5" via .score-tier-num (sighted-only; AT
   already gets the same fact from the wrapper's aria-label)
3. Per-tier dot SHAPE — round/rounded-square/square/diamond/
   empty-ring — so even at print resolution or in B&W the visual
   tier is distinguishable

This also adds a forced-colors media query so Windows High
Contrast keeps the dots visible without relying on the per-tier
color tokens (audit Finding #20 / #352, partially addressed here
for the score widget specifically).
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src/cf_knowledge_kiln/api/templates"
# #342 split: the score widget rules + forced-colors block moved
# from _results.css into the new _excerpt_score.css partial. Grep
# the new location so this test pins behavior, not the old layout.
_RESULTS_CSS = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_excerpt_score.css"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


class TestTemplateRendersTierNumber:
    """The template must emit a .score-tier-num span carrying 'N/5'."""

    def test_score_tier_num_span_present_in_source(self) -> None:
        """Source-grep the template (rendering needs a full fixture
        cascade)."""
        results_html = (_TEMPLATES / "_results.html").read_text()
        assert 'class="score-tier-num"' in results_html, (
            "_results.html must render a .score-tier-num span so "
            "color-blind users have a numeric tier prefix."
        )
        # The span renders the tier value over 5.
        assert "{{ r.score_tier }}/5" in results_html

    def test_score_tier_num_is_aria_hidden(self) -> None:
        """AT already gets the tier from the wrapper's aria-label;
        the visible span is sighted-redundancy only, so aria-hidden
        prevents double-announcement."""
        results_html = (_TEMPLATES / "_results.html").read_text()
        idx = results_html.index('class="score-tier-num"')
        # Window covers the opening tag.
        open_lt = results_html.rfind("<", 0, idx)
        close_gt = results_html.find(">", idx)
        tag = results_html[open_lt : close_gt + 1]
        assert 'aria-hidden="true"' in tag


class TestCssCarriesShapeVariantsPerTier:
    """Each tier must have a distinct shape so color isn't the
    sole differentiator. We grep for the per-tier shape rules."""

    def _css(self) -> str:
        return _RESULTS_CSS.read_text()

    def test_tier_4_has_rounded_square_shape(self) -> None:
        css = self._css()
        # Loose match: tier 4 modifies border-radius to something
        # smaller than 50% (round).
        assert ".score-tier-4 .score-dot" in css
        # Grab the rule body.
        idx = css.index(".score-tier-4 .score-dot")
        body = css[idx : idx + 200]
        assert "border-radius:" in body
        # Should be less than 50% (not round).
        assert "border-radius: 50%" not in body

    def test_tier_3_has_square_shape(self) -> None:
        css = self._css()
        assert ".score-tier-3 .score-dot" in css
        idx = css.index(".score-tier-3 .score-dot")
        body = css[idx : idx + 200]
        assert "border-radius:" in body

    def test_tier_2_has_diamond_shape(self) -> None:
        css = self._css()
        assert ".score-tier-2 .score-dot" in css
        idx = css.index(".score-tier-2 .score-dot")
        body = css[idx : idx + 300]
        # Diamond is a rotated square with border-radius: 0.
        assert "transform: rotate(45deg)" in body
        assert "border-radius: 0" in body

    def test_tier_1_uses_empty_ring_for_on_state(self) -> None:
        """Tier 1 (below floor) — even the "on" dot is hollow.
        That hollow-vs-filled distinction works in B&W."""
        css = self._css()
        assert ".score-tier-1 .score-dot-on" in css
        idx = css.index(".score-tier-1 .score-dot-on")
        body = css[idx : idx + 200]
        assert "background: transparent" in body


class TestForcedColorsSafety:
    """Windows High Contrast mode strips per-tier color tokens; the
    rules must remap to system tokens so the dots stay visible.
    Audit Finding #20 — full forced-colors coverage is #352; this
    addresses the score-widget piece specifically."""

    def test_forced_colors_media_query_present(self) -> None:
        css = _RESULTS_CSS.read_text()
        assert "@media (forced-colors: active)" in css

    def test_dots_remap_to_system_canvastext(self) -> None:
        css = _RESULTS_CSS.read_text()
        # The forced-colors block must use CanvasText (the system
        # text color in WHC); without this the dots vanish.
        idx = css.index("@media (forced-colors: active)")
        block = css[idx : idx + 1000]
        assert "CanvasText" in block
        assert "forced-color-adjust: none" in block
