"""#408 F2 + F18 — score + authority legends.

Five surfaces under test:

* :func:`api.views.score_legend_tiers` — 5-row ladder, strong-first.
* :func:`api.views.authority_vocabulary` — 9-row vocabulary, ordered.
* ``_results.html`` template — renders the ``<details>`` legend
  block once per result-list (NOT per card).
* ``_legends.css`` partial — exists, ships the chrome rules, has
  forced-colors + reduced-motion blocks.
* Bundle order — Makefile pulls ``_legends.css`` after
  ``_excerpt_score.css`` (where ``.score-dot`` lives) so the
  legend chips inherit the right styling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cf_knowledge_kiln.api.views import authority_vocabulary, score_legend_tiers

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates"
_LEGENDS_CSS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln" / "_legends.css"
_MAKEFILE = _REPO / "Makefile"


class TestScoreLegendTiers:
    """Score-tier vocabulary. The legend reads strong → weak so a
    user scans top-down as a quality ladder."""

    def test_returns_5_rows(self) -> None:
        rows = score_legend_tiers()
        assert len(rows) == 5

    def test_strong_first(self) -> None:
        """Tier 5 (strong) is the first row; tier 1 (below floor)
        is the last. This is a contract for the legend's reading
        direction — pin it so a future re-order is deliberate."""
        rows = score_legend_tiers()
        assert rows[0][0] == 5
        assert rows[-1][0] == 1

    def test_every_tier_has_label(self) -> None:
        rows = score_legend_tiers()
        for tier, label in rows:
            assert isinstance(tier, int)
            assert isinstance(label, str)
            assert label, f"tier {tier} has empty label"

    def test_tiers_are_complete(self) -> None:
        """All 5 tiers 1..5 appear exactly once. A missing tier
        would leave a gap in the visual ladder."""
        rows = score_legend_tiers()
        tiers = sorted(t for t, _ in rows)
        assert tiers == [1, 2, 3, 4, 5]


class TestAuthorityVocabulary:
    """The 9 recognized authority bands, in descending-authority
    order so the legend reads top-down: canonical → community →
    experimental."""

    def test_returns_9_rows(self) -> None:
        rows = authority_vocabulary()
        assert len(rows) == 9

    def test_platform_first(self) -> None:
        """Platform = canonical owner-team doc; should be the first
        row a new user sees so they learn the high-authority shape
        before the lower-authority ones."""
        rows = authority_vocabulary()
        assert rows[0][0] == "platform"

    def test_experimental_last(self) -> None:
        """Experimental = under evaluation; lowest authority. Last
        row in the legend."""
        rows = authority_vocabulary()
        assert rows[-1][0] == "experimental"

    def test_every_entry_has_tooltip(self) -> None:
        """Each row carries the sentence-shape editorial gloss from
        :data:`_AUTHORITY_TOOLTIPS`. Empty tooltip would render as
        an unhelpful bare chip."""
        rows = authority_vocabulary()
        for authority, tooltip in rows:
            assert isinstance(authority, str) and authority
            assert isinstance(tooltip, str)
            assert tooltip.endswith("."), f"{authority}: tooltip not sentence-cased"
            assert tooltip.lower().startswith(authority), (
                f"{authority}: tooltip should lead with the authority word, got {tooltip!r}"
            )


class TestResultsTemplateRendersLegend:
    """The legend renders once at the top of the results list when
    results are present, and not at all on the empty state."""

    @pytest.fixture
    def env(self) -> object:
        import jinja2

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=True,
        )
        # Register the two helpers as globals so the template
        # iteration works without route-side plumbing.
        env.globals["score_legend_tiers"] = score_legend_tiers
        env.globals["authority_vocabulary"] = authority_vocabulary
        return env

    def _result(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "chunk_id": "c1",
            "document_id": "d1",
            "title": "T",
            "excerpt_html": "x",
            "excerpt_full_html": "x",
            "heading_path": [],
            "heading_path_str": "",
            "repo": "owner/repo",
            "path": "doc.md",
            "source_url": None,
            "owner": None,
            "status": "active",
            "last_reviewed": None,
            "score": 0.5,
            "score_tier": 3,
            "deprecation_label": None,
            "status_tooltip": "Current — the canonical version.",
            "warnings": [],
        }
        base.update(overrides)
        return base

    def _render(self, env: object, results: list) -> str:
        return env.get_template("_results.html").render(  # type: ignore[attr-defined]
            query="x",
            results=results,
            warnings=[],
            query_id=None,
            filters={},
            selected_statuses=["active"],
        )

    def test_legend_renders_when_results_present(self, env: object) -> None:
        body = self._render(env, [self._result()])
        assert 'class="results-legend"' in body
        assert "results-legend-toggle" in body
        # Both vocabulary sections render.
        assert ">Relevance<" in body
        assert ">Authority<" in body

    def test_legend_absent_on_empty_results(self, env: object) -> None:
        """The empty state has its own teaching surface (kiln-empty.js
        onboarding). The legend would be noise there."""
        body = self._render(env, [])
        assert "results-legend" not in body

    def test_legend_renders_only_once_for_multi_results(self, env: object) -> None:
        """Per-list, not per-card. Multiple results should not
        repeat the legend block."""
        body = self._render(env, [self._result(chunk_id=f"c{i}") for i in range(3)])
        # ``results-legend-toggle`` is a unique class only on the
        # legend's <summary>. Counting it counts legend instances.
        assert body.count('class="results-legend-toggle"') == 1

    def test_legend_iterates_all_9_authorities(self, env: object) -> None:
        body = self._render(env, [self._result()])
        for authority in (
            "platform",
            "security",
            "standards",
            "compliance",
            "ops",
            "engineering",
            "operator",
            "community",
            "experimental",
        ):
            assert f"authority-{authority}" in body, f"legend missing {authority} authority chip"

    def test_legend_iterates_all_5_tiers(self, env: object) -> None:
        body = self._render(env, [self._result()])
        # The tier-num span renders ``N/5`` for each row 1..5.
        for tier in range(1, 6):
            assert f"{tier}/5" in body, f"legend missing tier {tier}"


class TestLegendsCssPartial:
    """The CSS partial ships the chrome rules + the a11y blocks."""

    def test_partial_exists(self) -> None:
        assert _LEGENDS_CSS.exists()

    def test_partial_styles_summary_toggle(self) -> None:
        css = _LEGENDS_CSS.read_text(encoding="utf-8")
        assert ".results-legend-toggle" in css

    def test_partial_styles_open_state_marker(self) -> None:
        """The ::before marker rotates when [open] — pin both rules
        so a refactor that drops one leaves the marker stuck."""
        css = _LEGENDS_CSS.read_text(encoding="utf-8")
        assert ".results-legend-toggle::before" in css
        assert ".results-legend[open] .results-legend-toggle::before" in css

    def test_partial_has_focus_visible_block(self) -> None:
        """Keyboard-accessible by default. Must have an explicit
        :focus-visible rule so the focus ring is consistent across
        UA defaults."""
        css = _LEGENDS_CSS.read_text(encoding="utf-8")
        assert ".results-legend-toggle:focus-visible" in css

    def test_partial_has_forced_colors_block(self) -> None:
        css = _LEGENDS_CSS.read_text(encoding="utf-8")
        assert "@media (forced-colors: active)" in css

    def test_partial_has_prefers_contrast_block(self) -> None:
        css = _LEGENDS_CSS.read_text(encoding="utf-8")
        assert "@media (prefers-contrast: more)" in css

    def test_partial_has_prefers_reduced_motion_block(self) -> None:
        css = _LEGENDS_CSS.read_text(encoding="utf-8")
        assert "@media (prefers-reduced-motion: reduce)" in css


class TestBundleOrder:
    """``_legends.css`` must concatenate AFTER ``_excerpt_score.css``
    (which owns ``.score-dot``) so the legend's score chips inherit
    the existing styling rather than fighting it."""

    def test_legends_bundled_after_excerpt_score(self) -> None:
        mk = _MAKEFILE.read_text(encoding="utf-8")
        excerpt_pos = mk.find("_excerpt_score.css")
        legends_pos = mk.find("_legends.css")
        assert excerpt_pos != -1, "_excerpt_score.css missing from Makefile"
        assert legends_pos != -1, "_legends.css missing from Makefile bundle"
        assert excerpt_pos < legends_pos, (
            "_legends.css must concatenate after _excerpt_score.css so the "
            "legend score chips inherit .score-dot styling."
        )
