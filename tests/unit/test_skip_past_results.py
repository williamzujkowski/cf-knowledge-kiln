"""Unit tests for the 'Skip past results' a11y link (UX-audit MEDIUM).

A full result page has 20 cards x ~6 inner controls (title button
+ expand + copy + 6 feedback buttons) = 120+ tab stops between
the search input and the colophon. Keyboard-only users have to
Tab through every one. The skip-link lets them jump in a single
Tab.

These tests pin:

* The link is rendered BEFORE the results section (otherwise the
  user has already tabbed past every card before reaching the
  link).
* The link's href targets the anchor at the end of the result
  list.
* The anchor element exists at the post-results position with
  tabindex=-1 so focus lands programmatically on activation.
* The existing 'Skip to results' link is unchanged (regression
  guard).
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest


@pytest.fixture
def env() -> jinja2.Environment:
    templates_dir = (
        Path(__file__).resolve().parents[2] / "src" / "cf_knowledge_kiln" / "api" / "templates"
    )
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    env.globals["feedback_categories"] = lambda: ()
    return env


def _render(env: jinja2.Environment) -> str:
    return env.get_template("search.html").render(
        request=None,
        query="",
        initial_results=None,
        filters={
            "repo": "",
            "doc_type": [],
            "owner": "",
            "last_reviewed_after": "",
            "tags": "",
        },
        rail_active_count=0,
    )


def test_skip_past_results_link_present(env: jinja2.Environment) -> None:
    """The link must render in the search page so keyboard users
    can jump past the result list in a single Tab."""
    body = _render(env)
    assert 'href="#after-results"' in body
    assert "Skip past results" in body


def test_skip_past_results_uses_skip_link_class(env: jinja2.Environment) -> None:
    """Reuses the existing .skip-link class — same hidden-until-
    focus treatment as 'Skip to results'. No new CSS rule needed."""
    body = _render(env)
    # Match the new link specifically (not the existing one).
    assert 'class="skip-link" href="#after-results"' in body


def test_anchor_target_exists(env: jinja2.Environment) -> None:
    """The anchor #after-results must exist at the end of the
    result region so the skip-link's href lands somewhere
    focusable. tabindex=-1 lets programmatic focus succeed
    without polluting the tab order."""
    body = _render(env)
    assert 'id="after-results"' in body
    # tabindex=-1 — not in tab order, but focusable via href.
    assert 'tabindex="-1"' in body


def test_skip_link_renders_before_results_section(env: jinja2.Environment) -> None:
    """The link MUST come before the results region in DOM order;
    otherwise the user has already tabbed past every card by the
    time the link is reachable, defeating the purpose."""
    body = _render(env)
    link_pos = body.index('href="#after-results"')
    results_pos = body.index('id="results"')
    assert link_pos < results_pos, (
        "skip-past-results link must render BEFORE the results section (currently after)"
    )


def test_anchor_renders_after_results_grid(env: jinja2.Environment) -> None:
    """The anchor must come AFTER the result section closes —
    landing the user past the cards. If it sat inside the grid
    the focus would still be in the middle of the result list."""
    body = _render(env)
    anchor_pos = body.index('id="after-results"')
    results_close_pos = body.index("</section>")
    assert anchor_pos > results_close_pos


def test_existing_skip_to_results_link_preserved(env: jinja2.Environment) -> None:
    """Regression guard: the OTHER skip link (#main, 'Skip to
    results') is the entry-point skip — pin it stays."""
    body = _render(env)
    assert 'href="#main"' in body
    assert "Skip to results" in body
