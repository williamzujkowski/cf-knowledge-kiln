"""Unit tests for the filter-rail open/closed + badge rendering (#273).

Renders ``search.html`` directly through Jinja against the same
template loader the FastAPI app uses, against a small synthetic
context dict. This isolates the template logic from the route
machinery so a regression here doesn't get masked by a 200 + body
that just lacks the new attribute.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest


@pytest.fixture
def env() -> jinja2.Environment:
    """A Jinja env pointed at the real templates directory.

    Matches Starlette's :class:`Jinja2Templates` defaults (HTML
    autoescape on, ``{% extends %}`` follows from the same loader).
    """
    templates_dir = (
        Path(__file__).resolve().parents[2] / "src" / "cf_knowledge_kiln" / "api" / "templates"
    )
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    # base.html calls ``url_for('static', path='kiln.css')`` (a
    # Starlette helper); stub it so the template renders without the
    # full FastAPI test client. We only assert on the filter rail
    # markup, not the head <link> output.
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    return env


def _base_context(**overrides: object) -> dict[str, object]:
    """Minimum context to render ``search.html`` end-to-end."""
    ctx: dict[str, object] = {
        # base.html reads request for CSRF + CSP middleware; we don't
        # exercise those branches so a None placeholder is enough.
        "request": None,
        "query": "",
        "initial_results": None,
        "filters": {
            "repo": "",
            "doc_type": [],
            "owner": "",
            "last_reviewed_after": "",
            "tags": "",
        },
        "rail_active_count": 0,
    }
    ctx.update(overrides)
    return ctx


def test_rail_renders_closed_with_no_active_count(env: jinja2.Environment) -> None:
    """Default render: no ``open`` attribute, no count chip, no
    ``filter-rail-active`` modifier class. Pins the negative path."""
    body = env.get_template("search.html").render(_base_context())
    # The details opening tag does not carry the open attribute.
    assert 'class="filter-rail"' in body
    assert 'class="filter-rail filter-rail-active"' not in body
    # Count chip absent.
    assert "filter-rail-count" not in body
    assert "active filter" not in body


def test_rail_renders_open_when_count_is_nonzero(env: jinja2.Environment) -> None:
    """Active filter: open attribute + active modifier class + chip
    text. All three signals together so a regression that strips ONE
    of them (e.g. a CSS-class-only edit that forgets the open
    attribute) trips this test.

    The aria-label on the <details> is dynamic — it includes the
    count so screen-reader users hear what sighted users see. A
    static "More filters" label would overshadow the visible chip
    for AT (reviewer-flagged a11y bug from the first iteration)."""
    body = env.get_template("search.html").render(
        _base_context(
            filters={
                "repo": "platform",
                "doc_type": [],
                "owner": "",
                "last_reviewed_after": "",
                "tags": "",
            },
            rail_active_count=1,
        )
    )
    assert 'class="filter-rail filter-rail-active"' in body
    assert "open>" in body  # the details element's > comes right after open
    assert "filter-rail-count" in body
    # The visual chip is hidden from AT (aria-hidden="true") so the
    # dynamic details aria-label is the single source of truth for
    # the count announcement.
    assert "· 1 active</span>" in body
    assert 'aria-label="More filters · 1 active filter"' in body


def test_rail_chip_uses_plural_when_count_greater_than_one(
    env: jinja2.Environment,
) -> None:
    """``2 active filters`` (plural) when count > 1; mirrors the
    English voice of every other count in the UI ('12 results').
    The plural is in the AT-only aria-label; the visible chip uses
    the compact ``· 2 active`` shorthand."""
    body = env.get_template("search.html").render(
        _base_context(
            filters={
                "repo": "platform",
                "doc_type": ["runbook"],
                "owner": "",
                "last_reviewed_after": "",
                "tags": "",
            },
            rail_active_count=2,
        )
    )
    assert "· 2 active</span>" in body
    assert 'aria-label="More filters · 2 active filters"' in body


def test_rail_aria_label_omits_count_when_no_active_filters(
    env: jinja2.Environment,
) -> None:
    """Default render: aria-label is the bare disclosure name — no
    "0 active filters" leak to AT users (would be noise). Pins the
    no-count path of the dynamic aria-label expression."""
    body = env.get_template("search.html").render(_base_context())
    assert 'aria-label="More filters"' in body
    assert "active filter" not in body


def test_rail_renders_closed_when_count_missing_from_context(
    env: jinja2.Environment,
) -> None:
    """Forward-compat: if a future render path forgets to thread
    ``rail_active_count``, the template falls back to 0 (rail closed)
    rather than UndefinedError. The ``| default(0)`` filter does the
    work; this test pins that contract."""
    ctx = _base_context()
    del ctx["rail_active_count"]
    body = env.get_template("search.html").render(ctx)
    assert 'class="filter-rail"' in body
    assert "filter-rail-count" not in body
