"""Unit tests for the colophon agent-link discoverability (#314 fix-3)."""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest


@pytest.fixture
def env() -> jinja2.Environment:
    templates_dir = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "cf_knowledge_kiln"
        / "api"
        / "templates"
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
    assert 'rel="noopener"' in body


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
