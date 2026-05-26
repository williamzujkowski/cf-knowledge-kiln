"""Unit tests for the feedback-ack template a11y refactor (#314 fix-1).

The pre-#314 template carried ``role="status"`` on a swapped
fragment, which is unreliable — AT live regions must exist at
swap-time to be observed. PR #314 moved the announcement path
through the persistent ``#search-status`` live region; the
fragment is now pure visual chrome with ``data-feedback-ack`` +
``data-signal`` attributes the JS dispatch reads.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

from cf_knowledge_kiln.api.views import feedback_categories


@pytest.fixture
def env() -> jinja2.Environment:
    templates_dir = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "cf_knowledge_kiln"
        / "api"
        / "templates"
    )
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )


def _render(env: jinja2.Environment, signal: str) -> str:
    return env.get_template("_feedback_ack.html").render(signal=signal)


def test_ack_does_not_carry_role_status(env: jinja2.Environment) -> None:
    """A fragment born WITH role='status' is not a reliable live
    region — the AT must observe the role BEFORE the swap. PR #314
    moved announcement to #search-status; pin that the fragment
    doesn't accidentally re-acquire the role."""
    body = _render(env, "useful")
    assert 'role="status"' not in body


def test_ack_carries_data_feedback_ack_marker(env: jinja2.Environment) -> None:
    """The JS dispatch in kiln-app.js matches on this attribute to
    decide when to announce. Without it, the ack swap is a no-op
    for AT users."""
    body = _render(env, "useful")
    assert "data-feedback-ack" in body


@pytest.mark.parametrize("signal", [c[0] for c in feedback_categories()])
def test_ack_carries_raw_signal_in_data_signal(
    env: jinja2.Environment, signal: str
) -> None:
    """data-signal is the RAW signal enum value (e.g.
    'duplicate_or_conflicting'), not the display label
    ('duplicate'). The JS announcement reformats it
    (underscores → spaces) at announcement time."""
    body = _render(env, signal)
    assert f'data-signal="{signal}"' in body


def test_ack_visible_chip_text_unchanged(env: jinja2.Environment) -> None:
    """Sighted users still see the same visual ack — only the AT
    path moved. The visible text 'Thanks — logged as <signal>'
    is preserved."""
    body = _render(env, "useful")
    assert "Thanks" in body
    assert "useful" in body
    assert "fb-ack-icon" in body
