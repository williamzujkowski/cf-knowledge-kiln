"""Pins the #347 fix (reset half): the Reset filters button.

Audit Finding #8: filter rail had no 'reset' affordance. The
'· N active' count tells the user how many filters they have but
offers no one-click undo. This PR adds a Reset filters button that
renders conditionally on _rail_active so it disappears when no
filters are set (no visible chrome that does nothing).

Implementation: native <button type=reset> clears the inputs to
form-defaults; kiln-app.js re-fires the HTMX submit so the
results update with the cleared filters. Graceful degradation:
without JS the click still clears the form (native reset);
the search just doesn't auto-refresh.

URL-shareable filter state is the OTHER half of #347 — filed
separately because it requires GET param parsing on the server,
URL push-state on the client, and history-back handling that's a
larger PR. This PR ships only the reset affordance.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src/cf_knowledge_kiln/api/templates"
_KILN_APP_JS = _REPO / "src/cf_knowledge_kiln/api/static/kiln-app.js"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


def _render(env: jinja2.Environment, rail_active_count: int) -> str:
    return env.get_template("search.html").render(
        request=None,
        query=None,
        initial_results=None,
        filters={},
        rail_active_count=rail_active_count,
    )


class TestResetButtonConditionalRender:
    def test_renders_when_filters_active(self, env: jinja2.Environment) -> None:
        body = _render(env, rail_active_count=2)
        assert 'data-action="reset-filters"' in body
        assert 'type="reset"' in body
        assert "Reset filters" in body

    def test_hidden_when_no_filters(self, env: jinja2.Environment) -> None:
        """No visible chrome that does nothing — when the user has no
        filters set, the reset button is absent."""
        body = _render(env, rail_active_count=0)
        assert "Reset filters" not in body
        assert 'data-action="reset-filters"' not in body

    def test_button_is_type_reset_for_graceful_degradation(self, env: jinja2.Environment) -> None:
        """type=reset guarantees the form clears even when JS is off
        (or the kiln-app.js dispatcher hasn't loaded yet). The
        JS re-submission is the enhancement, not the contract."""
        body = _render(env, rail_active_count=1)
        # Find the reset button's opening tag.
        idx = body.index('data-action="reset-filters"')
        open_lt = body.rfind("<", 0, idx)
        close_gt = body.find(">", idx)
        tag = body[open_lt : close_gt + 1]
        assert 'type="reset"' in tag


class TestJsResetDispatcher:
    def test_kiln_app_handles_reset_filters_action(self) -> None:
        """The data-action dispatch in kiln-app.js must include
        the reset-filters branch; without it the native reset
        clears the form but the result list stays stale."""
        source = _KILN_APP_JS.read_text()
        assert 'action === "reset-filters"' in source

    def test_resubmit_via_htmx_if_available(self) -> None:
        """When HTMX is loaded, use its programmatic trigger so the
        request goes through the same path as a regular submit
        (carries hx-vals, hx-headers, etc)."""
        source = _KILN_APP_JS.read_text()
        # The dispatcher branch must use window.htmx.trigger.
        idx = source.index('action === "reset-filters"')
        block = source[idx : idx + 1000]
        assert "window.htmx" in block
        assert ".trigger" in block

    def test_falls_back_to_native_submit_when_htmx_absent(self) -> None:
        """Graceful degradation: if HTMX isn't loaded (slow CDN,
        script-src CSP block), still re-submit the form via
        dispatchEvent so the form's native handler runs."""
        source = _KILN_APP_JS.read_text()
        idx = source.index('action === "reset-filters"')
        # Widen window — the formatter may wrap the Event(...) call
        # across lines.
        block = source[idx : idx + 1500]
        assert "dispatchEvent" in block
        assert 'new Event("submit"' in block

    def test_resubmit_is_inside_raf(self) -> None:
        """The browser applies the native form-reset on the same
        tick as the click; without rAF the re-submit could see
        the pre-reset values."""
        source = _KILN_APP_JS.read_text()
        idx = source.index('action === "reset-filters"')
        block = source[idx : idx + 1000]
        assert "requestAnimationFrame" in block
