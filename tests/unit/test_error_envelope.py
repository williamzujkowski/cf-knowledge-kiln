"""Unit tests for the ErrorResponse envelope (#258).

The handlers in :mod:`cf_knowledge_kiln.api.error_handlers` turn every
non-2xx response into the same JSON shape. Tests assert the wire
contract: error_code enum, retry_safe + retry_after_seconds on the
right error classes, request_id propagation from the middleware,
Retry-After header preservation, and the global Exception handler's
shape.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from cf_knowledge_kiln.api.error_handlers import (
    install_error_handlers,
    raise_with_code,
)
from cf_knowledge_kiln.api.errors import (
    ErrorResponse,
    default_for_status,
)
from cf_knowledge_kiln.api.request_id import install_request_id_middleware


def _build_app() -> FastAPI:
    """Tiny app with the middleware + handlers under test.

    Each route exercises a distinct error path so tests can assert the
    envelope shape independently of the real /v1/* surface.
    """
    app = FastAPI()
    install_request_id_middleware(app)
    install_error_handlers(app)

    @app.get("/raise-bare-http-503")
    async def _bare_503() -> None:
        raise HTTPException(status_code=503, detail="bare prose")

    @app.get("/raise-with-code-rate-limit")
    async def _rate_limit() -> None:
        raise_with_code(
            status_code=429,
            error_code="rate_limited",
            message="slow down",
            retry_after_seconds=12,
            headers={"Retry-After": "12"},
        )

    @app.get("/raise-with-code-generator")
    async def _gen() -> None:
        raise_with_code(
            status_code=503,
            error_code="generator_unavailable",
            message="enable models.generator in config",
        )

    @app.get("/raise-generic")
    async def _generic() -> None:
        raise RuntimeError("boom")

    @app.post("/validate-body")
    async def _validate_body(payload: dict[str, int]) -> dict[str, int]:
        # Returning identity exercises the Pydantic body validation
        # path — bad payload triggers RequestValidationError before
        # the handler runs.
        return payload

    return app


# ─── default_for_status ──────────────────────────────────────────


class TestDefaultForStatus:
    def test_401_maps_to_auth_required(self) -> None:
        code, retry_safe, _ = default_for_status(401)
        assert code == "auth_required"
        assert retry_safe is False

    def test_429_is_retry_safe(self) -> None:
        code, retry_safe, _ = default_for_status(429)
        assert code == "rate_limited"
        assert retry_safe is True

    def test_503_is_retry_safe_with_default_delay(self) -> None:
        code, retry_safe, retry_after = default_for_status(503)
        assert code == "db_unreachable"
        assert retry_safe is True
        assert retry_after == 30

    def test_unmapped_status_falls_to_internal_error(self) -> None:
        code, retry_safe, _ = default_for_status(418)
        assert code == "internal_error"
        assert retry_safe is False


# ─── HTTPException → envelope ─────────────────────────────────────


class TestHttpExceptionHandler:
    def test_bare_http_503_envelope_shape(self) -> None:
        """A bare HTTPException with no override produces the
        status-default envelope. Envelope is JSON; status_code matches."""
        with TestClient(_build_app()) as client:
            resp = client.get("/raise-bare-http-503")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error_code"] == "db_unreachable"
        assert body["retry_safe"] is True
        assert body["retry_after_seconds"] == 30
        # request_id is set by the middleware → echoed in envelope.
        assert body["request_id"] is not None
        # Same value as the response header — caller correlation.
        assert resp.headers["X-Request-ID"] == body["request_id"]

    def test_raise_with_code_overrides_status_default(self) -> None:
        """raise_with_code can set error_code different from the
        status's default mapping (e.g. 503 → generator_unavailable,
        not db_unreachable)."""
        with TestClient(_build_app()) as client:
            resp = client.get("/raise-with-code-generator")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error_code"] == "generator_unavailable"
        # No retry_after_seconds set by the route → envelope reflects
        # the operator-action-required nature.
        assert body["retry_after_seconds"] is None

    def test_rate_limit_preserves_retry_after_header(self) -> None:
        with TestClient(_build_app()) as client:
            resp = client.get("/raise-with-code-rate-limit")
        assert resp.status_code == 429
        # Retry-After header preserved for HTTP-aware clients.
        assert resp.headers["Retry-After"] == "12"
        body = resp.json()
        assert body["error_code"] == "rate_limited"
        assert body["retry_safe"] is True
        assert body["retry_after_seconds"] == 12

    def test_envelope_includes_message(self) -> None:
        with TestClient(_build_app()) as client:
            resp = client.get("/raise-bare-http-503")
        assert resp.json()["message"] == "bare prose"


# ─── RequestValidationError → 422 envelope ────────────────────────


class TestValidationErrorHandler:
    def test_invalid_body_produces_envelope_with_errors(self) -> None:
        with TestClient(_build_app()) as client:
            resp = client.post("/validate-body", json={"not": "an int"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "invalid_request"
        # Pydantic per-field errors surfaced in detail.errors so the
        # agent can locate the bad field.
        assert "errors" in body["detail"]
        assert isinstance(body["detail"]["errors"], list)


# ─── Generic Exception → 500 internal_error ───────────────────────


class TestGenericExceptionHandler:
    def test_unhandled_exception_produces_500_envelope(self) -> None:
        with TestClient(_build_app(), raise_server_exceptions=False) as client:
            resp = client.get("/raise-generic")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error_code"] == "internal_error"
        # Generic shape is intentionally vague — doesn't leak the
        # 'boom' message or the traceback.
        assert "boom" not in body["message"]
        assert body["request_id"] is not None
        assert resp.headers["X-Request-ID"] == body["request_id"]


# ─── ErrorResponse model ──────────────────────────────────────────


class TestErrorResponseModel:
    def test_required_fields(self) -> None:
        e = ErrorResponse(error_code="db_unreachable", message="x")
        # Pydantic defaults populate the optional fields.
        assert e.retry_safe is False
        assert e.retry_after_seconds is None
        assert e.request_id is None
        assert e.detail is None

    def test_extra_fields_rejected(self) -> None:
        """extra='forbid' so callers don't accidentally smuggle
        unrecognized fields through. Stable wire shape."""
        import pytest

        with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError
            ErrorResponse(
                error_code="db_unreachable",
                message="x",
                unrecognized="oops",  # type: ignore[call-arg]
            )
