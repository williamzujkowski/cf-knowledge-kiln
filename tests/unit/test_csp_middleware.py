"""Unit tests for the strict-CSP middleware (#144).

Covers:

* ``build_policy()`` returns the canonical directive string and
  includes every directive a reviewer expects to see.
* The middleware adds the enforcing header by default.
* ``KILN_CSP_REPORT_ONLY=1`` flips the header name to the
  ``-Report-Only`` variant without touching the directive payload.
* The middleware doesn't clobber a CSP header an inner handler
  explicitly set (setdefault, not assignment).

No DB, no network, no app factory — these tests construct a minimal
FastAPI app inline so they stay in the unit suite per AGENTS.md.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.csp import build_policy, install_csp_middleware


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip the report-only env var so per-test state doesn't leak."""
    monkeypatch.delenv("KILN_CSP_REPORT_ONLY", raising=False)
    yield


# ─── build_policy() shape ──────────────────────────────────────────


def test_build_policy_includes_every_expected_directive() -> None:
    """The policy carries the full list of directives the spec mandates."""
    policy = build_policy()
    # Every directive the issue called out must appear.
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ):
        assert directive in policy, f"missing directive: {directive!r}"


def test_build_policy_joins_with_semicolons() -> None:
    """Directives are joined with `; ` per the CSP spec grammar."""
    policy = build_policy()
    # There are 10 directives → 9 separators.
    assert policy.count("; ") == 9
    # No leftover newlines or stray whitespace.
    assert "\n" not in policy
    assert not policy.startswith(" ")
    assert not policy.endswith(" ")


def test_build_policy_does_not_emit_unsafe_inline() -> None:
    """The whole point of the rollout: no `unsafe-inline`, no `unsafe-eval`."""
    policy = build_policy()
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy


# ─── Middleware behavior ───────────────────────────────────────────


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    def probe() -> PlainTextResponse:
        return PlainTextResponse("ok")

    install_csp_middleware(app)
    return app


def test_middleware_adds_enforcing_header_by_default(clean_env: None) -> None:
    """Default mode adds `Content-Security-Policy`, not the report-only variant."""
    with TestClient(_build_app()) as client:
        response = client.get("/probe")
    assert response.status_code == 200
    assert "Content-Security-Policy" in response.headers
    assert "Content-Security-Policy-Report-Only" not in response.headers
    assert response.headers["Content-Security-Policy"] == build_policy()


def test_middleware_emits_report_only_header_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`KILN_CSP_REPORT_ONLY=1` flips the header name."""
    monkeypatch.setenv("KILN_CSP_REPORT_ONLY", "1")
    with TestClient(_build_app()) as client:
        response = client.get("/probe")
    assert response.status_code == 200
    assert "Content-Security-Policy-Report-Only" in response.headers
    assert "Content-Security-Policy" not in response.headers
    # Payload is identical; only the header name differs.
    assert response.headers["Content-Security-Policy-Report-Only"] == build_policy()


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "on", "1"])
def test_middleware_accepts_truthy_report_only_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Defense in depth — accept the same truthy values pydantic-settings would."""
    monkeypatch.setenv("KILN_CSP_REPORT_ONLY", value)
    with TestClient(_build_app()) as client:
        response = client.get("/probe")
    assert "Content-Security-Policy-Report-Only" in response.headers


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "anything"])
def test_middleware_rejects_falsy_report_only_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Anything other than an explicit truthy literal stays in enforcement mode."""
    monkeypatch.setenv("KILN_CSP_REPORT_ONLY", value)
    with TestClient(_build_app()) as client:
        response = client.get("/probe")
    assert "Content-Security-Policy" in response.headers
    assert "Content-Security-Policy-Report-Only" not in response.headers


def test_middleware_does_not_overwrite_handler_set_header(clean_env: None) -> None:
    """An inner handler can stamp its own CSP and the middleware respects it.

    Keeps the door open for per-route policy tweaks (e.g. a docs page
    that wants `style-src 'self' 'unsafe-inline'` for Swagger) without
    changing the middleware itself.
    """
    app = FastAPI()

    @app.get("/custom")
    def custom() -> PlainTextResponse:
        return PlainTextResponse(
            "ok",
            headers={"Content-Security-Policy": "default-src 'none'"},
        )

    install_csp_middleware(app)

    with TestClient(app) as client:
        response = client.get("/custom")
    assert response.headers["Content-Security-Policy"] == "default-src 'none'"
