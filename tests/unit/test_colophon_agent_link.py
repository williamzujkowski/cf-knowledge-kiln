"""Unit tests for the colophon agent-link discoverability (#314 fix-3)."""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest


@pytest.fixture
def env() -> jinja2.Environment:
    templates_dir = (
        Path(__file__).resolve().parents[2] / "src" / "cf_knowledge_kiln" / "api" / "templates"
    )
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


def _render(env: jinja2.Environment) -> str:
    return env.get_template("base.html").render(request=None)


def test_existing_developer_links_preserved(env: jinja2.Environment) -> None:
    """Regression guard: /docs, /openapi.json, /healthz must still
    appear in the colophon nav."""
    body = _render(env)
    assert '<a href="/docs">API docs</a>' in body
    assert '<a href="/openapi.json">OpenAPI</a>' in body
    assert '<a href="/healthz">Health</a>' in body


def test_agent_link_omitted_when_helper_returns_none(
    env: jinja2.Environment,
) -> None:
    """KILN_AGENT_GUIDE_URL unset → no link, no orphan separator."""
    env.globals["agent_guide_url"] = lambda: None
    body = _render(env)
    assert "/v1/agent/context-pack" not in body
    nav_match = re.search(
        r'<nav aria-label="Developer resources">(.*?)</nav>',
        body,
        re.DOTALL,
    )
    assert nav_match is not None
    middots = nav_match.group(1).count("·")
    assert middots == 2, f"expected 2 middots (3 links), got {middots}"


def test_agent_link_rendered_when_helper_returns_url(
    env: jinja2.Environment,
) -> None:
    env.globals["agent_guide_url"] = lambda: "https://example.com/agent-guide"
    body = _render(env)
    assert "https://example.com/agent-guide" in body
    assert "/v1/agent/context-pack" in body
    # rel must include both noopener (no window.opener handle for the
    # destination page) AND noreferrer (no Referer header leak to a
    # potentially external operator-configured URL).
    assert 'rel="noopener noreferrer"' in body


def test_agent_link_inside_developer_resources_nav(
    env: jinja2.Environment,
) -> None:
    env.globals["agent_guide_url"] = lambda: "https://example.com/g"
    body = _render(env)
    nav_match = re.search(
        r'<nav aria-label="Developer resources">(.*?)</nav>',
        body,
        re.DOTALL,
    )
    assert nav_match is not None
    assert "/v1/agent/context-pack" in nav_match.group(1)


def test_agent_link_adds_third_separator_when_present(
    env: jinja2.Environment,
) -> None:
    env.globals["agent_guide_url"] = lambda: "https://example.com/g"
    body = _render(env)
    nav_match = re.search(
        r'<nav aria-label="Developer resources">(.*?)</nav>',
        body,
        re.DOTALL,
    )
    assert nav_match is not None
    middots = nav_match.group(1).count("·")
    assert middots == 3, f"expected 3 middots (4 links), got {middots}"


# ─── URL-scheme validation (blind-review HIGH finding) ─────────


class TestAgentGuideUrlSchemeValidation:
    """The colophon renders the configured URL directly into an
    ``href`` attribute. Operator misconfiguration or an env-injection
    attack could set the value to ``javascript:`` / ``data:`` and turn
    the link into an XSS vector — Jinja's autoescape sanitizes HTML,
    not URL schemes. Pin the allow-list behavior."""

    def _agent_url(self, raw: str | None) -> str | None:
        """Drive the real implementation by patching the setting and
        calling the production helper — proves the validation lives
        in the helper, not in test stubs."""
        from cf_knowledge_kiln.api.views import agent_guide_url
        from cf_knowledge_kiln.config import get_settings

        get_settings.cache_clear()
        try:
            import os

            if raw is None:
                os.environ.pop("KILN_AGENT_GUIDE_URL", None)
            else:
                os.environ["KILN_AGENT_GUIDE_URL"] = raw
            return agent_guide_url()
        finally:
            os.environ.pop("KILN_AGENT_GUIDE_URL", None)
            get_settings.cache_clear()

    def test_unset_returns_default_swagger_anchor(self) -> None:
        """#357: when the env var is unset, the helper returns the
        same-origin Swagger anchor so the colophon link is always
        discoverable. Operators who want the link OFF set
        KILN_AGENT_GUIDE_URL=disabled (see the test below)."""
        assert self._agent_url(None) == "/docs#tag/agent"

    def test_disabled_sentinel_returns_none(self) -> None:
        """Explicit off-switch for operators who want the colophon
        clean. The sentinel string is documented in the helper's
        docstring; any other no-link request now requires the
        sentinel rather than an empty env var."""
        assert self._agent_url("disabled") is None

    def test_https_url_is_accepted(self) -> None:
        url = "https://docs.example/agent-guide"
        assert self._agent_url(url) == url

    def test_http_url_is_accepted(self) -> None:
        url = "http://docs.example/agent-guide"
        assert self._agent_url(url) == url

    def test_absolute_path_is_accepted(self) -> None:
        # Same-origin absolute paths are safe — no scheme to abuse.
        url = "/docs/agent-integration"
        assert self._agent_url(url) == url

    def test_javascript_scheme_is_refused(self) -> None:
        # The headline XSS vector.
        assert self._agent_url("javascript:alert(1)") is None

    def test_data_scheme_is_refused(self) -> None:
        assert self._agent_url("data:text/html,<script>alert(1)</script>") is None

    def test_vbscript_scheme_is_refused(self) -> None:
        assert self._agent_url("vbscript:msgbox(1)") is None

    def test_protocol_relative_url_is_refused(self) -> None:
        # ``//evil.example/x`` inherits the page's scheme but jumps to
        # an attacker host — refuse and let the operator be explicit.
        assert self._agent_url("//evil.example/agent-guide") is None

    def test_mailto_scheme_is_refused(self) -> None:
        assert self._agent_url("mailto:agent@example.com") is None

    def test_whitespace_only_falls_back_to_default(self) -> None:
        """#357: whitespace-only env var means "operator didn't really
        set anything"; fall back to the default so the link still
        renders. The explicit off-switch is the ``disabled`` sentinel."""
        assert self._agent_url("   ") == "/docs#tag/agent"

    def test_leading_whitespace_is_trimmed(self) -> None:
        # An operator pasting a URL might leave whitespace; we honor
        # the URL once it's stripped — without revisiting the scheme
        # check.
        url = "  https://docs.example/agent-guide  "
        assert self._agent_url(url) == "https://docs.example/agent-guide"
