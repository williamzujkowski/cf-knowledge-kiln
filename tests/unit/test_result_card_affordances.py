"""Unit tests for visible expand + copy affordances on result cards (#284).

Renders ``_results.html`` directly through Jinja with a synthetic
single-result context. Asserts:

* Each card carries a ``copy-citation`` button alongside the
  source-line, with ``data-action="copy-citation"`` so the
  existing kiln-keys.js dispatcher picks it up.
* Each card carries a ``toggle-expand`` button alongside the
  excerpt, with ``data-action="toggle-expand"``.
* Each button shows a small ``<kbd>`` hint with the keyboard
  shortcut letter so power users learn the binding.
* Both buttons are inside the ``.result-card`` so the dispatcher's
  ``.closest('.result-card')`` walk lands them on the right card.
"""

from __future__ import annotations

from pathlib import Path
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


def test_copy_citation_button_renders(env: jinja2.Environment) -> None:
    """Every result card has a 'copy' button with the data-action
    the dispatcher in kiln-keys.js routes to copyCitation()."""
    body = _render(env, _result())
    assert 'data-action="copy-citation"' in body
    # Visible copy in the button text.
    assert ">copy" in body


def test_toggle_expand_button_renders(env: jinja2.Environment) -> None:
    """Every result card has an 'expand' button with the data-action
    the dispatcher routes to toggleExpand()."""
    body = _render(env, _result())
    assert 'data-action="toggle-expand"' in body
    # Default label is 'expand' (the card starts collapsed).
    assert ">expand" in body


def test_buttons_show_kbd_hint(env: jinja2.Environment) -> None:
    """The shortcut letter renders as a small <kbd> hint so power
    users learn the binding. Without this, only keyboard-discovery
    via the `?` cheatsheet would surface it.

    The <kbd> carries a class for typographic treatment; we match
    the closing tag content instead of an attribute-free `<kbd>X</kbd>`
    so the class can move/rename without breaking the test."""
    body = _render(env, _result())
    # Both shortcut letters present in a <kbd> element. Use the
    # class-name-anchored pattern so the test stays robust against
    # future class renames.
    assert ">c</kbd>" in body
    assert ">o</kbd>" in body


def test_buttons_are_inside_result_card(env: jinja2.Environment) -> None:
    """The dispatcher does element.closest('.result-card') to find
    the card the click acts on. The buttons MUST be descendants of
    .result-card or the closest() walk misses them and the click
    is a no-op."""
    body = _render(env, _result())
    card_start = body.index('class="result-card')
    card_end = body.index("</li>", card_start)
    card_block = body[card_start:card_end]
    assert 'data-action="copy-citation"' in card_block
    assert 'data-action="toggle-expand"' in card_block


def test_expand_button_carries_label_flip_data_attributes(
    env: jinja2.Environment,
) -> None:
    """The 'expand' button label flips to 'collapse' on click. JS
    reads the alternates from data-label-collapsed / data-label-
    expanded on the button itself — copy lives in the template,
    not in JS. Pin the attributes so a future template refactor
    that drops them silently breaks the label flip."""
    body = _render(env, _result())
    assert 'data-label-collapsed="expand"' in body
    assert 'data-label-expanded="collapse"' in body


def test_expand_button_label_is_in_separate_span(
    env: jinja2.Environment,
) -> None:
    """JS swaps the inner .card-action-label span text — NOT the
    button's full textContent — so the <kbd> hint survives the
    flip. The label must be wrapped in a span the JS query can
    find."""
    body = _render(env, _result())
    # The expand button's inner structure: a label span + the kbd.
    assert '<span class="card-action-label">expand</span>' in body


def test_buttons_are_type_button_not_submit(env: jinja2.Environment) -> None:
    """The buttons sit inside the feedback <form> on the same card
    (#119 / #122 made the title and feedback widgets buttons).
    type='button' is mandatory so a click doesn't accidentally
    submit the feedback form."""
    import re

    body = _render(env, _result())
    for action in ("copy-citation", "toggle-expand"):
        # Find the opening <button ...> tag with the action and
        # confirm it carries type="button".
        m = re.search(rf'<button\b[^>]*data-action="{action}"[^>]*>', body, re.DOTALL)
        assert m is not None, f"missing button for {action}"
        assert 'type="button"' in m.group(0), f"button {action} must be type='button' not submit"
