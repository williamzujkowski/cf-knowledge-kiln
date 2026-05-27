"""Unit tests for the #336 authority band on result cards.

Two surfaces:

* :func:`api.views.authority_tooltip` — pins the
  ``authority → tooltip`` contract, mirrors
  :func:`status_tooltip` / :func:`deprecation_label`.
* ``_results.html`` template — renders the ``.authority-band``
  chip only when ``r.authority`` is truthy, threads the tooltip
  + AT aria-label, and falls back gracefully on unknown values.

The chip is positioned in the source-line between owner and
``last_reviewed`` so the reading order is *source/path — by owner ·
authority — Reviewed yyyy-mm-dd*. Tests pin that positional
contract too — moving the chip is a deliberate design change, not
something a refactor should silently do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import pytest

from cf_knowledge_kiln.api.views import authority_tooltip

# ─── helper-function contract ────────────────────────────────────────


class TestAuthorityTooltip:
    """Pin the (authority → tooltip) contract."""

    def test_platform(self) -> None:
        assert authority_tooltip("platform") == "Platform — canonical owner-team doc."

    def test_security(self) -> None:
        assert authority_tooltip("security") == (
            "Security — hardening or threat-model authoritative."
        )

    def test_standards(self) -> None:
        assert authority_tooltip("standards") == "Standards — org-wide policy."

    def test_operator(self) -> None:
        """Lower-authority value still gets a gloss so the AT user
        gets the same disambiguation a sighted user gets on hover."""
        assert authority_tooltip("operator") == ("Operator — team-curated, not org-wide canonical.")

    def test_unknown_returns_none(self) -> None:
        """Corpus-native authority values outside the kiln-
        recommended set (e.g. ``slack-handbook``) get no tooltip.
        The template falls back to rendering the raw chip — better
        than a guess."""
        assert authority_tooltip("slack-handbook") is None
        assert authority_tooltip("canonical") is None

    def test_empty_returns_none(self) -> None:
        assert authority_tooltip("") is None

    def test_none_returns_none(self) -> None:
        """The view layer passes ``getattr(ref, 'authority', None)``
        through unchanged — the helper must accept ``None`` so the
        template's ``{% if r.authority %}`` is the single guard."""
        assert authority_tooltip(None) is None

    @pytest.mark.parametrize(
        "authority",
        [
            "platform",
            "security",
            "standards",
            "compliance",
            "ops",
            "engineering",
            "operator",
            "community",
            "experimental",
        ],
    )
    def test_tooltip_is_sentence_shaped(self, authority: str) -> None:
        """All recognized tooltips are single-sentence, capitalized,
        period-terminated — matches the editorial voice of
        ``status_tooltip`` + ``deprecation_label``."""
        tooltip = authority_tooltip(authority)
        assert tooltip is not None
        assert tooltip[0].isupper(), f"not sentence-cased: {tooltip!r}"
        assert tooltip.endswith("."), f"missing terminal period: {tooltip!r}"

    @pytest.mark.parametrize(
        "authority",
        [
            "platform",
            "security",
            "standards",
            "compliance",
            "ops",
            "engineering",
            "operator",
            "community",
            "experimental",
        ],
    )
    def test_tooltip_leads_with_authority_word(self, authority: str) -> None:
        """Each tooltip leads with the Title-cased authority word so
        the AT announcement reads ``Authority: platform — Platform
        — …``, echoing the visible chip for context."""
        tooltip = authority_tooltip(authority)
        assert tooltip is not None
        assert tooltip.lower().startswith(authority), tooltip


# ─── template contract ───────────────────────────────────────────────


@pytest.fixture
def env() -> jinja2.Environment:
    templates_dir = (
        Path(__file__).resolve().parents[2] / "src" / "cf_knowledge_kiln" / "api" / "templates"
    )
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )


def _result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "title": "Example",
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


def _render(env: jinja2.Environment, result: dict[str, Any]) -> str:
    return env.get_template("_results.html").render(
        query="x",
        results=[result],
        warnings=[],
        query_id=None,
        filters={},
        selected_statuses=["active"],
    )


class TestAuthorityBandTemplate:
    def test_band_renders_when_authority_present(self, env: jinja2.Environment) -> None:
        body = _render(
            env,
            _result(
                authority="platform",
                authority_tooltip="Platform — canonical owner-team doc.",
            ),
        )
        assert "authority-band" in body
        assert "authority-platform" in body
        # Visible label text.
        assert ">platform<" in body

    def test_band_absent_when_authority_none(self, env: jinja2.Environment) -> None:
        """The chip is conditional. Cards without a declared
        authority must render without an empty span — clutter on
        a result list of 20 is a real cost."""
        body = _render(env, _result(authority=None, authority_tooltip=None))
        assert "authority-band" not in body

    def test_band_absent_when_field_missing(self, env: jinja2.Environment) -> None:
        """Older fixtures / pre-#336 callers don't supply the field.
        The ``is defined`` guard must keep them rendering. This is
        the same pattern #337 (chunk_index) needed."""
        result = _result()
        result.pop("authority", None)
        result.pop("authority_tooltip", None)
        body = _render(env, result)
        assert "authority-band" not in body

    def test_tooltip_threaded_to_data_attr(self, env: jinja2.Environment) -> None:
        """The AT + hover/focus disambiguation lives on
        ``data-tooltip`` (#296 keyboard-accessible) — title= was
        retired because it only fires on mouse hover."""
        body = _render(
            env,
            _result(
                authority="security",
                authority_tooltip="Security — hardening or threat-model authoritative.",
            ),
        )
        assert 'data-tooltip="Security' in body

    def test_aria_label_mirrors_tooltip(self, env: jinja2.Environment) -> None:
        """Per #296 review: AT users get the same disambiguation
        sighted users get on hover/focus. The aria-label must
        carry the authority value AND the gloss."""
        body = _render(
            env,
            _result(
                authority="platform",
                authority_tooltip="Platform — canonical owner-team doc.",
            ),
        )
        assert 'aria-label="Authority: platform — Platform' in body

    def test_unknown_authority_renders_without_tooltip(self, env: jinja2.Environment) -> None:
        """Corpus-native authority values keep rendering — the chip
        is a graceful fallback, not a recognized-values gate."""
        body = _render(env, _result(authority="slack-handbook", authority_tooltip=None))
        assert "authority-band" in body
        # No data-tooltip on the chip (the AT user still gets the
        # raw value via aria-label).
        assert 'aria-label="Authority: slack-handbook"' in body

    def test_band_positioned_between_owner_and_freshness(self, env: jinja2.Environment) -> None:
        """Reading order: source/path — by owner · authority —
        Reviewed yyyy-mm-dd. Moving the chip is a deliberate design
        change, not something a refactor should silently do."""
        body = _render(
            env,
            _result(
                owner="platform-team",
                last_reviewed="2026-04-01",
                authority="platform",
                authority_tooltip="Platform — canonical owner-team doc.",
            ),
        )
        owner_pos = body.find("platform-team")
        chip_pos = body.find("authority-band")
        freshness_pos = body.find("Reviewed 2026-04-01")
        assert -1 < owner_pos < chip_pos < freshness_pos, (
            f"unexpected order: owner={owner_pos}, chip={chip_pos}, freshness={freshness_pos}"
        )


# ─── CSS rule presence ───────────────────────────────────────────────


_EXCERPT_SCORE_CSS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cf_knowledge_kiln"
    / "api"
    / "static"
    / "kiln"
    / "_excerpt_score.css"
)


class TestAuthorityBandCSS:
    def test_rule_present(self) -> None:
        """The base ``.authority-band`` rule must be in the partial
        the Makefile concatenates — otherwise the chip renders
        unstyled in production."""
        css = _EXCERPT_SCORE_CSS.read_text()
        assert ".authority-band" in css

    def test_high_authority_accent_present(self) -> None:
        """Platform / security / standards / compliance get the
        teal accent. Pin the selector so the high-authority lineup
        is the canonical 'this is the authoritative one' set."""
        css = _EXCERPT_SCORE_CSS.read_text()
        assert ".authority-band.authority-platform" in css
        assert ".authority-band.authority-security" in css
        assert ".authority-band.authority-standards" in css

    def test_forced_colors_fallback_present(self) -> None:
        """#352 baseline: every editorial chip must keep working
        under Windows High Contrast. The CanvasText/Highlight
        fallback rule must be in this partial."""
        css = _EXCERPT_SCORE_CSS.read_text()
        # Same windowed-grep pattern as test_forced_colors_full_pass:
        # require the @media block AND a matching CanvasText rule
        # for the chip within ~800 chars of the band selector.
        idx = css.find(".authority-band")
        window = css[idx : idx + 4000]
        assert "@media (forced-colors: active)" in window
        assert "CanvasText" in window
