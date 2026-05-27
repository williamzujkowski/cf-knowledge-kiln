"""Pins the #348 fix: cheatsheet uses aria-labelledby (not aria-label).

A screen reader that lands on the cheatsheet dialog used to announce
both:
1. The dialog's aria-label ("Keyboard shortcuts"), then
2. The visible <h2> ("Keyboard")

Two announcements for one logical label. The fix points
aria-labelledby at the h2's id so the visible heading IS the dialog's
announcement — no double-speak.

Adding new shortcuts (f, gg/G, r) is tracked separately because those
require keyboard handlers + form-state hooks that intersect with #347
(reset filters + URL state).
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


def _render(env: jinja2.Environment) -> str:
    return env.get_template("search.html").render(
        request=None,
        query=None,
        initial_results=None,
        filters={},
        rail_active_count=0,
    )


class TestCheatsheetLabelling:
    def test_dialog_uses_aria_labelledby_not_aria_label(self, env: jinja2.Environment) -> None:
        body = _render(env)
        # Isolate the cheatsheet dialog's opening tag (between
        # id="cheatsheet" and the next '>').
        idx = body.index('id="cheatsheet"')
        open_lt = body.rfind("<", 0, idx)
        close_gt = body.find(">", idx)
        opening_tag = body[open_lt : close_gt + 1]
        # The dialog MUST reference the heading by id …
        assert 'aria-labelledby="cheatsheet-title"' in opening_tag, (
            "Cheatsheet dialog must use aria-labelledby pointing at the visible <h2>."
        )
        # … and MUST NOT also carry aria-label (double-announce).
        assert "aria-label=" not in opening_tag, (
            "Cheatsheet dialog must NOT carry aria-label — it's already "
            "labelled by the heading via aria-labelledby."
        )

    def test_heading_has_matching_id(self, env: jinja2.Environment) -> None:
        body = _render(env)
        # The h2 inside the cheatsheet must carry id="cheatsheet-title".
        assert 'id="cheatsheet-title"' in body
        # Sanity: the id sits on the h2, not on some other element.
        idx = body.index('id="cheatsheet-title"')
        open_lt = body.rfind("<", 0, idx)
        tag_start = body[open_lt : open_lt + 3]
        assert tag_start == "<h2", "cheatsheet-title id must be on the <h2>"

    def test_close_button_still_has_explicit_label(self, env: jinja2.Environment) -> None:
        """Regression guard: the close button SHOULD still have its
        own aria-label ('Close shortcuts'). It carries a multiplication-
        sign glyph as visible text which isn't a label AT can announce
        meaningfully."""
        body = _render(env)
        assert 'aria-label="Close shortcuts"' in body
