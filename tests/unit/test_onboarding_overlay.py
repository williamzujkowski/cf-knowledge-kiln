"""Pins the #338 onboarding overlay.

A first-time visitor on `/` lands on five rows of chrome before
the search input; the four exemplar queries are buried under the
fold and there's no sentence explaining what the kiln is. The
overlay surfaces one paragraph + two action buttons on first
visit, persists a localStorage flag to suppress on subsequent
visits, and gracefully degrades:
* No JS → overlay never renders interactively (data-hidden) — the
  existing exemplars cover the no-JS path.
* prefers-reduced-motion → JS skips showing the dialog (the scrim
  fade IS the motion we'd otherwise show).
* Native <dialog> + showModal() unavailable → JS bails out.

Tests pin contract strings the JS reads + template attributes the
JS expects.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src/cf_knowledge_kiln/api/templates"
_KILN_EMPTY_JS = _REPO / "src/cf_knowledge_kiln/api/static/kiln-empty.js"
_ONBOARDING_CSS = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_onboarding.css"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


def _render_base(env: jinja2.Environment) -> str:
    return env.get_template("base.html").render(request=None)


class TestTemplateContract:
    def test_overlay_renders_in_base(self, env: jinja2.Environment) -> None:
        """Including _onboarding.html in base.html means every page
        gets the overlay markup. The JS decides whether to show it."""
        body = _render_base(env)
        assert 'id="onboarding"' in body

    def test_overlay_is_a_native_dialog(self, env: jinja2.Environment) -> None:
        """Native <dialog> gives Esc-close + focus-trap for free."""
        body = _render_base(env)
        # Find the onboarding element + check its opening tag.
        idx = body.index('id="onboarding"')
        open_lt = body.rfind("<", 0, idx)
        # The opening tag must be <dialog ...> not <div ...>.
        assert body[open_lt : open_lt + 7] == "<dialog"

    def test_overlay_starts_data_hidden(self, env: jinja2.Environment) -> None:
        """The overlay must render data-hidden so JS-off browsers
        never see it interactively — the empty-state exemplars are
        the fallback path."""
        body = _render_base(env)
        idx = body.index('id="onboarding"')
        close_gt = body.find(">", idx)
        tag = body[idx : close_gt + 1]
        assert "data-hidden" in tag

    def test_overlay_uses_aria_labelledby(self, env: jinja2.Environment) -> None:
        """Same labelling pattern as the cheatsheet (#348) — the
        visible heading IS the dialog's accessible name. No
        aria-label to avoid double-announcement."""
        body = _render_base(env)
        idx = body.index('id="onboarding"')
        close_gt = body.find(">", idx)
        tag = body[idx : close_gt + 1]
        assert 'aria-labelledby="onboarding-title"' in tag
        # Regression guard: no aria-label (double-announce).
        assert "aria-label=" not in tag

    def test_two_action_buttons_present(self, env: jinja2.Environment) -> None:
        body = _render_base(env)
        assert 'data-action="onboarding-try"' in body
        assert 'data-action="onboarding-skip"' in body


class TestJsContract:
    def _source(self) -> str:
        return _KILN_EMPTY_JS.read_text()

    def test_localstorage_key_present(self) -> None:
        source = self._source()
        assert 'ONBOARDING_KEY = "kiln.onboarding-seen.v1"' in source

    def test_show_helper_reads_localstorage_flag(self) -> None:
        """showOnboardingIfFresh must check the flag before showing."""
        source = self._source()
        assert "showOnboardingIfFresh" in source
        assert "getItem(ONBOARDING_KEY)" in source

    def test_show_helper_checks_prefers_reduced_motion(self) -> None:
        """The scrim fade IS the motion the user opted out of;
        skip the dialog entirely under reduced-motion."""
        source = self._source()
        start = source.index("showOnboardingIfFresh")
        body = source[start : start + 2000]
        assert "prefers-reduced-motion: reduce" in body
        assert "matchMedia" in body

    def test_show_helper_feature_detects_dialog(self) -> None:
        """Native <dialog> + showModal() availability — without it
        skip silently rather than throw."""
        source = self._source()
        start = source.index("showOnboardingIfFresh")
        body = source[start : start + 2000]
        assert "showModal" in body
        # And the typeof guard
        assert "typeof dlg.showModal" in body

    def test_dismiss_persists_flag(self) -> None:
        """The dismiss handler MUST setItem(ONBOARDING_KEY) so the
        next visit suppresses the overlay."""
        source = self._source()
        assert "dismissOnboarding" in source
        start = source.index("dismissOnboarding")
        body = source[start : start + 800]
        assert "setItem(ONBOARDING_KEY" in body

    def test_skip_button_dispatched(self) -> None:
        """data-action='onboarding-skip' branch in the click listener."""
        source = self._source()
        assert 'action === "onboarding-skip"' in source

    def test_try_button_focuses_input(self) -> None:
        """The Try button closes the overlay AND focuses #query so
        the user has a concrete next thing to do."""
        source = self._source()
        # Find the try branch.
        idx = source.index('action === "onboarding-try"')
        body = source[idx : idx + 1000]
        assert 'getElementById("query")' in body
        assert "input.focus()" in body

    def test_show_called_on_dom_ready(self) -> None:
        """The DOMContentLoaded init must call showOnboardingIfFresh."""
        source = self._source()
        idx = source.index("DOMContentLoaded")
        body = source[idx : idx + 500]
        assert "showOnboardingIfFresh()" in body


class TestCssContract:
    def _css(self) -> str:
        return _ONBOARDING_CSS.read_text()

    def test_data_hidden_attribute_hides(self) -> None:
        """data-hidden hides the dialog so JS-off users never see it."""
        css = self._css()
        assert ".onboarding[data-hidden]" in css
        # Within that rule expect display:none. Comment inside the
        # rule body pushes the property past the first 200 chars —
        # widen the window to 700 to cover it.
        idx = css.index(".onboarding[data-hidden]")
        body = css[idx : idx + 700]
        assert "display: none" in body

    def test_reduced_motion_kills_animations(self) -> None:
        """Belt-and-braces — JS skips showing entirely under
        reduced-motion, but if it ever evaluates wrong (browser
        quirk), the CSS still kills the scrim + card animations."""
        css = self._css()
        assert "@media (prefers-reduced-motion: reduce)" in css
        idx = css.index("@media (prefers-reduced-motion: reduce)")
        body = css[idx : idx + 600]
        assert "animation: none" in body

    def test_mobile_stacks_buttons_vertically(self) -> None:
        """Below 480px the two action buttons stack so each touch
        target hits 44px without crowding."""
        css = self._css()
        assert "@media (max-width: 480px)" in css
        idx = css.index("@media (max-width: 480px)")
        body = css[idx : idx + 700]
        assert "flex-direction: column-reverse" in body
