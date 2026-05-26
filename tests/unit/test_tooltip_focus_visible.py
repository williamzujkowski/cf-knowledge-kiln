"""Unit tests for the focus-visible tooltip migration (#296).

The audit (post-PR-#292) flagged two problems with the native
``title=`` tooltips that #279 and #281 added:

1. **Keyboard users get no tooltip.** Native ``title=`` only fires
   on mouse hover. Keyboard-only sighted users (no mouse, no AT)
   miss every tooltip on the result card.
2. **Density overload.** Multiple ``title=`` attributes per card
   can compete for the same screen region.

The fix is a CSS-driven tooltip pattern using ``[data-tooltip]``
attributes that render on ``:hover`` AND ``:focus-visible``, so
keyboard users get the same disambiguation sighted-mouse users
get. The ``aria-label`` path stays in place for AT.

These tests pin the migration:

* The 4 tooltip surfaces on a result card use ``data-tooltip=``
  not ``title=``.
* The bundle has a ``[data-tooltip]`` CSS rule with both
  ``:hover`` and ``:focus-visible`` triggers.
* AT announcements (``aria-label``) preserved on the surfaces
  that carried them (status badge, feedback buttons).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "src" / "cf_knowledge_kiln" / "api" / "templates"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    # Feedback widget reads feedback_categories as a global.
    from cf_knowledge_kiln.api.views import feedback_categories

    env.globals["feedback_categories"] = feedback_categories
    return env


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
        "source_url": "https://example.com/doc",
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
        query_id="q-1",
        filters={},
        selected_statuses=["active"],
    )


class TestResultCardTooltipMigration:
    """Each tooltip surface on the result card uses data-tooltip,
    not the keyboard-inaccessible native title= attribute."""

    def test_status_badge_uses_data_tooltip(self, env: jinja2.Environment) -> None:
        """The status badge tooltip (added in #281) must migrate to
        data-tooltip so keyboard users get it on :focus-visible."""
        body = _render(env, _result())
        # Find the status-badge span and assert it carries
        # data-tooltip, not title.
        m = re.search(r'<span class="status-badge[^"]*"[^>]*>', body, re.DOTALL)
        assert m is not None, "status-badge not found in render"
        span = m.group(0)
        assert "data-tooltip=" in span
        assert "title=" not in span
        # aria-label preserved for AT.
        assert "aria-label=" in span

    def test_source_link_uses_data_tooltip(self, env: jinja2.Environment) -> None:
        """The source-link tooltip ('Open canonical source in a new
        tab') must migrate to data-tooltip — keyboard users
        tabbing through links should hear the same disambiguation."""
        body = _render(env, _result())
        # External source link.
        m = re.search(r'<a class="source source-link"[^>]*>', body, re.DOTALL)
        assert m is not None, "source-link not found in render"
        anchor = m.group(0)
        assert "data-tooltip=" in anchor
        assert "title=" not in anchor

    def test_score_widget_uses_data_tooltip(self, env: jinja2.Environment) -> None:
        """The score-widget tooltip explaining the 0.0-1.0 scale must
        migrate. Keyboard users tabbing to a result card need the
        same scale explanation."""
        body = _render(env, _result())
        m = re.search(r'<span class="score score-tier-\d+"[^>]*>', body, re.DOTALL)
        assert m is not None, "score widget not found"
        span = m.group(0)
        assert "data-tooltip=" in span
        assert "title=" not in span

    def test_feedback_buttons_use_data_tooltip(
        self,
        env: jinja2.Environment,
    ) -> None:
        """Each of the 6 feedback buttons (added in #279) must
        migrate. The disambiguation is the WHOLE POINT of the
        tooltip pattern for the feedback widget — without it the
        terse labels (yes/no/stale/wrong source/...) are
        ambiguous on first encounter."""
        body = _render(env, _result())
        # Find all feedback-link buttons.
        buttons = re.findall(r'<button[^>]*class="feedback-link"[^>]*>', body, re.DOTALL)
        assert len(buttons) == 6, f"expected 6 feedback buttons, got {len(buttons)}"
        for btn in buttons:
            assert "data-tooltip=" in btn, f"missing data-tooltip on: {btn[:80]!r}"
            assert "title=" not in btn, f"stale title= attr on: {btn[:80]!r}"
            # aria-label preserved for AT.
            assert "aria-label=" in btn

    def test_source_link_carries_aria_label_for_at(
        self,
        env: jinja2.Environment,
    ) -> None:
        """Reviewer-flagged BLOCKER: removing the native title=
        attribute also stripped the source-link's accessible name —
        screen readers heard only '{{ repo }}/{{ path }}' without the
        'open in new tab' disambiguation. The aria-label restores
        the AT path."""
        body = _render(env, _result())
        m = re.search(r'<a class="source source-link"[^>]*>', body, re.DOTALL)
        assert m is not None
        anchor = m.group(0)
        assert "aria-label=" in anchor, (
            "source-link must carry aria-label so AT users get the "
            "same disambiguation sighted users get on focus-visible"
        )
        # And the aria-label conveys 'open in new tab' so the AT
        # consumer knows the link's target context.
        assert "open canonical source in a new tab" in anchor.lower()

    def test_score_widget_carries_aria_label_for_at(
        self,
        env: jinja2.Environment,
    ) -> None:
        """Reviewer-flagged BLOCKER: the score widget's title= held the
        '0.0-1.0 scale, N of 5' explanation. Without aria-label, AT
        users heard 'score 0.823' with no scale context."""
        body = _render(env, _result(score=0.823, score_tier=4))
        m = re.search(r'<span class="score score-tier-\d+"[^>]*>', body, re.DOTALL)
        assert m is not None
        span = m.group(0)
        assert "aria-label=" in span, (
            "score widget must carry aria-label so AT users get the "
            "scale explanation sighted users get on focus-visible"
        )
        # The aria-label includes the numeric score and the tier.
        assert "0.823" in span
        assert "4 of 5" in span

    def test_unknown_status_still_omits_tooltip(self, env: jinja2.Environment) -> None:
        """For corpus-native statuses with no _STATUS_TOOLTIPS entry,
        the badge must still skip BOTH data-tooltip and title.
        Pin the negative case so the migration doesn't accidentally
        leak a tooltip-less data-tooltip='' (which would still
        trigger the CSS ::after empty box)."""
        body = _render(env, _result(status="reference", status_tooltip=None))
        m = re.search(r'<span class="status-badge status-reference"[^>]*>', body, re.DOTALL)
        assert m is not None
        span = m.group(0)
        assert "data-tooltip=" not in span
        assert "title=" not in span


class TestTooltipCss:
    """The CSS bundle must carry the [data-tooltip] rule with both
    :hover and :focus-visible triggers — without :focus-visible the
    migration is half-done and keyboard users still see nothing."""

    @pytest.fixture
    def kiln_css(self) -> str:
        bundle = REPO_ROOT / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln.css"
        return bundle.read_text(encoding="utf-8")

    def test_data_tooltip_rule_present(self, kiln_css: str) -> None:
        """The base [data-tooltip] rule MUST exist — that's the
        CSS hook the template-side migration depends on."""
        assert "[data-tooltip]" in kiln_css

    def test_focus_visible_trigger_present(self, kiln_css: str) -> None:
        """The whole point of the migration: keyboard users get the
        tooltip on :focus-visible, not just :hover. Match a rule
        body that includes :focus-visible alongside [data-tooltip]
        attribute reference."""
        # Match any rule that combines [data-tooltip] with
        # :focus-visible — covers either order
        # ([data-tooltip]:focus-visible::after or
        # [data-tooltip]:hover::after, [data-tooltip]:focus-visible::after).
        assert re.search(r"\[data-tooltip\][^{]*:focus-visible", kiln_css, re.DOTALL), (
            "no rule combines [data-tooltip] with :focus-visible — "
            "keyboard users won't see the tooltip on focus"
        )

    def test_hover_trigger_present(self, kiln_css: str) -> None:
        """Mouse hover MUST keep working — the migration doesn't
        DROP the hover trigger, just ADDS focus-visible."""
        assert re.search(r"\[data-tooltip\][^{]*:hover", kiln_css, re.DOTALL)

    def test_tooltip_pulls_text_from_attr(self, kiln_css: str) -> None:
        """The CSS reads the tooltip text via ``content: attr(data-tooltip)``
        so the value flows from the HTML attribute, not duplicated
        in CSS. Pin that contract."""
        assert "content: attr(data-tooltip)" in kiln_css
