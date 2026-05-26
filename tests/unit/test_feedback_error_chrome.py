"""Unit tests for the feedback error chrome (#293).

Two HIGH audit findings, landed together because they share the
retry-loop failure mode:

* The HTMX swap-allow list in ``kiln-app.js`` enumerates only
  400/404/429/503. A 500 / 502 / 504 from the server is dropped
  silently — the user sees nothing change. Extend the allow list.
* ``_feedback_error.html`` uses ``<details open>`` chrome that
  visually mirrors the unfilled feedback prompt. A user mistaking
  the error for the original prompt can click again, generating
  more failed writes → retry loop. Refactor to the editorial
  ``.notice .notice-alert`` shape from ``_error.html`` so all
  error fragments share the oxblood-rule italic voice.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ERROR_TEMPLATE = (
    REPO_ROOT / "src" / "cf_knowledge_kiln" / "api" / "templates" / "_feedback_error.html"
)
KILN_APP_JS = REPO_ROOT / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln-app.js"

# The audit-flagged required set. A 502 / 504 from a sluggish DB
# or transient upstream failure must surface to the user instead
# of being swallowed silently by HTMX's default isError behavior.
_REQUIRED_HTMX_SWAP_STATUSES: frozenset[int] = frozenset({400, 404, 429, 500, 502, 503, 504})


@pytest.fixture(scope="module")
def kiln_app_source() -> str:
    return KILN_APP_JS.read_text(encoding="utf-8")


class TestHtmxSwapAllowList:
    """The htmx:beforeSwap handler in kiln-app.js MUST mark every
    status in _REQUIRED_HTMX_SWAP_STATUSES as 'render the
    response body, don't error'. A regression that drops one
    silently breaks visible error rendering for that status."""

    @pytest.mark.parametrize("status", sorted(_REQUIRED_HTMX_SWAP_STATUSES))
    def test_status_in_swap_allow_list(self, kiln_app_source: str, status: int) -> None:
        """The handler tests the inbound status via a chain of
        ``s === N`` comparisons. Pin each required status appears
        as an exact-equality check so a drift (e.g. dropping the
        503 by accident) trips the test."""
        # Match `s === 500` or `s === 500 ||` — whitespace tolerant.
        pattern = re.compile(rf"s\s*===\s*{status}\b")
        assert pattern.search(kiln_app_source), (
            f"kiln-app.js htmx:beforeSwap missing `s === {status}` — "
            f"a {status} response from the server will be swallowed "
            "by HTMX's default isError behavior, leaving the user "
            "with no visible signal of the failure"
        )


class TestFeedbackErrorChrome:
    """The error fragment MUST NOT visually mirror the unfilled
    feedback prompt — that's the audit's documented retry-loop
    failure mode. Use the .notice .notice-alert shape from
    _error.html so all error fragments share one voice."""

    @pytest.fixture
    def env(self) -> jinja2.Environment:
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ERROR_TEMPLATE.parent)),
            autoescape=True,
        )

    def _render(self, env: jinja2.Environment, message: str = "Test error") -> str:
        return env.get_template("_feedback_error.html").render(message=message)

    def test_fragment_uses_notice_alert_chrome(self, env: jinja2.Environment) -> None:
        """The fragment renders with .notice + .notice-alert classes,
        reusing the editorial alert voice from _error.html. Sharing
        chrome means: oxblood left rule + italic body + role='alert'
        — a single visual treatment for all error fragments."""
        body = self._render(env)
        assert "notice" in body
        assert "notice-alert" in body

    def test_fragment_is_not_a_details_disclosure(
        self,
        env: jinja2.Environment,
    ) -> None:
        """The retry-loop bug came from <details open> chrome that
        mirrored the unfilled feedback prompt. Pin that no
        <details> element appears — the fragment must read as an
        alert, not a disclosure the user is meant to expand or
        re-interact with."""
        body = self._render(env)
        assert "<details" not in body
        assert "<summary" not in body

    def test_fragment_has_role_alert(self, env: jinja2.Environment) -> None:
        """role='alert' is the canonical AT signal for 'this is an
        error that just happened'. Without it, screen readers treat
        the swap as ordinary content."""
        body = self._render(env)
        assert 'role="alert"' in body

    def test_fragment_renders_the_message(self, env: jinja2.Environment) -> None:
        """The message passed in by the handler must appear in the
        rendered body — the user has to see WHAT went wrong."""
        body = self._render(env, message="Per-IP feedback rate limit exceeded.")
        assert "Per-IP feedback rate limit exceeded." in body

    def test_fragment_carries_no_feedback_form_chrome(
        self,
        env: jinja2.Environment,
    ) -> None:
        """The retry-loop mode the audit flagged was: error chrome
        looks like the feedback widget, user re-clicks. Pin that
        no .feedback-prompt or .feedback-link classes leak through
        — the error must read as an error, not a fresh prompt."""
        body = self._render(env)
        assert "feedback-prompt" not in body
        assert "feedback-link" not in body
