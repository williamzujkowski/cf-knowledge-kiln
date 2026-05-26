"""Unit tests for the status-badge tooltip rendering (#280).

Renders ``_results.html`` directly through Jinja with a synthetic
results context. Asserts each badge with a known status carries
both ``title`` (sighted hover) AND ``aria-label`` (AT
announcement) attributes — the conditional render in the
template doesn't accidentally drop one when the other is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import pytest
from markupsafe import escape


def _escape(s: str) -> str:
    return str(escape(s))


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
    """A minimum-shape result-card dict the _results.html template
    can render. Only the fields the badge block reads matter for
    these tests; rest are bare defaults to make the template happy.
    """
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
        "status_tooltip": None,
        "warnings": [],
    }
    base.update(overrides)
    return base


def _render_results(env: jinja2.Environment, result: dict[str, Any]) -> str:
    return env.get_template("_results.html").render(
        query="x",
        results=[result],
        warnings=[],
        query_id=None,
        filters={},
        selected_statuses=["active"],
    )


@pytest.mark.parametrize(
    "status,tooltip",
    [
        ("active", "Current — the canonical version."),
        ("approved", "Approved — reviewed and signed off."),
        ("draft", "Draft — not yet approved as authoritative."),
        (
            "deprecated",
            "Deprecated — superseded; do not cite as current.",
        ),
        ("archived", "Archived — kept for historical reference."),
        ("superseded", "Superseded — see the linked successor."),
    ],
)
def test_known_status_renders_title_and_aria_label(
    env: jinja2.Environment, status: str, tooltip: str
) -> None:
    """Every kiln-recommended status renders both attributes. The
    conditional `{% if r.status_tooltip %}` block in the template
    must emit both — never one without the other."""
    body = _render_results(env, _result(status=status, status_tooltip=tooltip))
    escaped_tip = _escape(tooltip)
    assert f'title="{escaped_tip}"' in body
    aria = _escape(f"{status}: {tooltip}")
    assert f'aria-label="{aria}"' in body


def test_deprecation_stamp_aria_hidden_when_status_tooltip_present(
    env: jinja2.Environment,
) -> None:
    """Belt-and-braces against the redundant AT-announcement bug the
    blind reviewer caught: with the status-badge now carrying an
    aria-label that subsumes the deprecation copy
    ('deprecated: Deprecated — superseded; do not cite as current.'),
    the deprecation-stamp must be aria-hidden so the AT user doesn't
    hear the same meaning twice. The visible stamp text is kept for
    sighted users — only the AT exposure is removed."""
    body = _render_results(
        env,
        _result(
            status="deprecated",
            status_tooltip="Deprecated — superseded; do not cite as current.",
            deprecation_label="Deprecated · do not cite",
        ),
    )
    # The visible stamp text is still rendered.
    assert "Deprecated · do not cite" in body
    # The stamp wrapper carries aria-hidden so AT skips it; the
    # status-badge's aria-label is the single source for the AT
    # announcement.
    assert 'class="deprecation-stamp" aria-hidden="true"' in body
    # And the stamp no longer carries a duplicate aria-label.
    assert 'class="deprecation-stamp" aria-label=' not in body


def test_unknown_status_omits_tooltip_attributes(
    env: jinja2.Environment,
) -> None:
    """A corpus-native status (e.g. 'reference' per #203) lands as
    status_tooltip=None. The badge still renders color-coded but
    without title/aria-label so we don't guess copy the operator
    never wrote down."""
    body = _render_results(env, _result(status="reference", status_tooltip=None))
    # Badge text still present.
    assert "status-reference" in body
    # No tooltip attributes — search inside the status-badge span
    # range to avoid false negatives from unrelated title/aria-label
    # uses on the same page.
    badge_start = body.index('class="status-badge status-reference"')
    badge_end = body.index(">", badge_start)
    badge_tag = body[badge_start:badge_end]
    assert "title=" not in badge_tag
    assert "aria-label=" not in badge_tag
