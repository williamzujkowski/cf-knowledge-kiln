"""Unit tests for preview-panel owner + last_reviewed rendering (#282).

Renders ``_preview.html`` directly through Jinja with a synthetic
context. Asserts:

* When ``doc.owner`` and ``doc.last_reviewed`` are present, they
  render as visible labeled spans in the preview meta line.
* When either is absent (``None`` / missing), the corresponding
  span is omitted — no empty "by " or "Reviewed " ghosts.

The data is already on the Document model; this PR only threads
it into the visible preview header. Pinning the template
contract here so a future refactor of the meta block can't
silently drop the freshness signal.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jinja2
import pytest


@pytest.fixture
def env() -> jinja2.Environment:
    templates_dir = (
        Path(__file__).resolve().parents[2] / "src" / "cf_knowledge_kiln" / "api" / "templates"
    )
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )


def _doc(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "title": "Example doc",
        "repo": "org/repo",
        "path": "docs/example.md",
        "status": "active",
        "owner": None,
        "last_reviewed": None,
        "source_url": None,
    }
    base.update(overrides)
    # Use SimpleNamespace so attribute access (doc.owner) works the
    # same way the live SQLAlchemy model row would expose it.
    return SimpleNamespace(**base)


def _render_preview(env: jinja2.Environment, doc: Any) -> str:
    return env.get_template("_preview.html").render(
        missing=False,
        doc=doc,
        target={
            "chunk_id": "chunk-1",
            "chunk_index": 0,
            "heading_path": [],
            "content": "x",
        },
        prev=[],
        next=[],
    )


def test_owner_renders_when_present(env: jinja2.Environment) -> None:
    """Owner is the 'who owns this doc' signal — when present, it
    must appear in the preview header so users don't scroll back
    to the result card."""
    body = _render_preview(env, _doc(owner="platform-team"))
    # The label "by " precedes the owner name (mirrors the result-
    # card source-line convention).
    assert "platform-team" in body
    # Em-italics around the owner (per the editorial voice).
    assert "<em>platform-team</em>" in body


def test_owner_omitted_when_none(env: jinja2.Environment) -> None:
    """No owner → no 'by ' span. Otherwise the meta line would
    carry a ghost label with no value."""
    body = _render_preview(env, _doc(owner=None))
    assert "preview-owner" not in body
    assert ">by " not in body


def test_last_reviewed_renders_when_present(env: jinja2.Environment) -> None:
    """Last-reviewed is the freshness signal — when present, it
    appears as a <time> element with the datetime attribute so
    AT users + parsers can both consume it."""
    reviewed = date(2026, 3, 15)
    body = _render_preview(env, _doc(last_reviewed=reviewed))
    # Visible "Reviewed YYYY-MM-DD" copy mirrors the result-card.
    assert "Reviewed 2026-03-15" in body
    # Machine-readable datetime attribute on the <time> element.
    assert 'datetime="2026-03-15"' in body


def test_last_reviewed_omitted_when_none(env: jinja2.Environment) -> None:
    """No reviewed date → no <time> element. The absence of the
    span is the signal that the doc has no review record (rather
    than a misleading 'Reviewed None' or 'Reviewed —')."""
    body = _render_preview(env, _doc(last_reviewed=None))
    assert "preview-freshness" not in body
    assert "Reviewed " not in body


def test_both_render_together(env: jinja2.Environment) -> None:
    """The two fields are independent — when both present, both
    render. This pins that adding one doesn't shadow the other
    (a future Jinja {% if/else %} refactor risk)."""
    body = _render_preview(
        env,
        _doc(owner="platform-team", last_reviewed=date(2026, 1, 1)),
    )
    assert "<em>platform-team</em>" in body
    assert "Reviewed 2026-01-01" in body
