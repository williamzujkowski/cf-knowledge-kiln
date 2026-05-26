"""Unit tests for the feedback-widget rendering (#278).

Renders ``_feedback_widget.html`` directly through Jinja with a
synthetic context. Asserts that each button carries both a
``title`` (sighted hover tooltip) AND an ``aria-label`` that
combines the visible label with the explanation (AT users hear
both).

The helper :func:`api.views.feedback_categories` is the single
source of truth — this test pins that the template actually
reads + renders that source rather than re-hard-coding labels.

Note on HTML escaping: Jinja autoescapes ``'`` → ``&#39;`` etc.,
so tooltip strings containing apostrophes (e.g. "This didn't
answer my question.") land in the rendered output as escaped
entities. The helper :func:`_escape` runs the same MarkupSafe
escape Jinja uses so comparisons are robust against that.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
from markupsafe import escape

from cf_knowledge_kiln.api.views import feedback_categories


def _escape(s: str) -> str:
    """HTML-escape the same way Jinja2 autoescape does."""
    return str(escape(s))


@pytest.fixture
def env() -> jinja2.Environment:
    templates_dir = (
        Path(__file__).resolve().parents[2] / "src" / "cf_knowledge_kiln" / "api" / "templates"
    )
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    # The template-under-test reads ``feedback_categories`` from the
    # template globals — same pattern as how the route wires the
    # helper into the Starlette templating env.
    env.globals["feedback_categories"] = feedback_categories
    return env


def _render_widget(env: jinja2.Environment) -> str:
    return env.get_template("_feedback_widget.html").render(
        chunk_id="chunk-abc",
        query_id="query-xyz",
    )


def test_widget_renders_six_buttons(env: jinja2.Environment) -> None:
    """Same six categories as the engine enum — pins that the
    template iterates the helper, not a private hard-coded list."""
    body = _render_widget(env)
    # Each button has class feedback-link; count them.
    assert body.count('class="feedback-link"') == 6


def test_widget_renders_data_tooltip_attribute_per_button(
    env: jinja2.Environment,
) -> None:
    """Every category's tooltip lands as a ``data-tooltip`` attribute
    (PR #296 migrated from ``title=`` so the CSS-driven pattern
    surfaces the tooltip on :focus-visible too, not just mouse
    :hover). Without this, the terse labels stay ambiguous on
    first encounter for keyboard-only sighted users — even worse
    than the original audit-flagged density problem."""
    body = _render_widget(env)
    for _signal, _label, tooltip in feedback_categories():
        escaped = _escape(tooltip)
        assert f'data-tooltip="{escaped}"' in body, (
            f"missing data-tooltip attribute for {tooltip!r} (escaped: {escaped!r})"
        )
        # And NO regression to the keyboard-inaccessible native
        # title= attribute that #296 replaced.
        assert f'title="{escaped}"' not in body, (
            f"stale title= attr for {tooltip!r} — should be data-tooltip after #296"
        )


def test_widget_aria_label_combines_label_and_tooltip(
    env: jinja2.Environment,
) -> None:
    """AT users hear ``"{label}: {tooltip}"`` — the visible label
    PLUS the explanation. A bare aria-label of just the label
    would tell AT users less than what sighted users get on
    hover."""
    body = _render_widget(env)
    for _signal, label, tooltip in feedback_categories():
        aria = _escape(f"{label}: {tooltip}")
        assert f'aria-label="{aria}"' in body, f"missing combined aria-label {aria!r}"


def test_widget_keeps_visible_label_text(env: jinja2.Environment) -> None:
    """The visible label text (between <button>...</button>) is
    unchanged from the pre-#278 design — sighted users still scan
    the same terse phrase. The tooltip is additive, not a
    replacement."""
    body = _render_widget(env)
    for _signal, label, _tooltip in feedback_categories():
        # The button body holds the label; assert it appears as a
        # direct child text node.
        assert f">{label}</button>" in body


def test_widget_signal_value_matches_engine(env: jinja2.Environment) -> None:
    """Each button's POST value is the engine's signal enum member.
    A drift here would render a button whose POST the server 422s."""
    body = _render_widget(env)
    for signal, _label, _tooltip in feedback_categories():
        assert f'value="{signal}"' in body
