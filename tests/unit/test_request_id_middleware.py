"""Unit tests for the X-Request-ID middleware (#260).

The middleware generates or honors an inbound ``X-Request-ID``
header, exposes it on ``request.state.request_id``, and echoes it
on the response. Critical for joining log lines, telemetry rows,
and error responses for a single user complaint to one correlation
key.
"""

from __future__ import annotations

import re

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from cf_knowledge_kiln.api.request_id import (
    HEADER,
    _sanitize,
    install_request_id_middleware,
    request_id_for,
)


def _app_returning_state_request_id() -> FastAPI:
    """Tiny FastAPI app whose /probe handler echoes request.state.request_id.

    Lets us assert the middleware stamped state without coupling to
    the real /v1/* surface.
    """
    app = FastAPI()
    install_request_id_middleware(app)

    @app.get("/probe")
    async def _probe(request: Request) -> dict[str, str | None]:
        return {"request_id": request_id_for(request)}

    return app


# ─── Sanitize ──────────────────────────────────────────────────────


class TestSanitize:
    def test_passes_valid_uuid_unchanged(self) -> None:
        assert _sanitize("abc123-def-456") == "abc123-def-456"

    def test_strips_whitespace(self) -> None:
        assert _sanitize("  abc  ") == "abc"

    def test_replaces_disallowed_chars(self) -> None:
        # Newline injection is the headline attack — scrub it.
        assert "\n" not in _sanitize("abc\ninjected")
        assert "\r" not in _sanitize("abc\rinjected")

    def test_empty_or_whitespace_only_returns_none(self) -> None:
        assert _sanitize("") is None
        assert _sanitize("   ") is None

    def test_caps_long_values(self) -> None:
        long = "a" * 500
        out = _sanitize(long)
        assert out is not None
        assert len(out) <= 200

    def test_special_chars_replaced_with_underscore(self) -> None:
        assert _sanitize("abc/def=ghi") == "abc_def_ghi"


# ─── Middleware ────────────────────────────────────────────────────


class TestMiddleware:
    def test_generates_uuid_when_no_header(self) -> None:
        with TestClient(_app_returning_state_request_id()) as client:
            resp = client.get("/probe")
        assert resp.status_code == 200
        rid = resp.json()["request_id"]
        # UUID4 shape: 36 chars with dashes at positions 8/13/18/23.
        assert rid is not None
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", rid
        )

    def test_honors_inbound_header(self) -> None:
        with TestClient(_app_returning_state_request_id()) as client:
            resp = client.get("/probe", headers={HEADER: "my-trace-123"})
        assert resp.status_code == 200
        assert resp.json()["request_id"] == "my-trace-123"

    def test_echoes_value_on_response_header(self) -> None:
        """Caller-correlation contract: the value we used INTERNALLY
        is the value we send back so the caller's log line and ours
        match."""
        with TestClient(_app_returning_state_request_id()) as client:
            resp = client.get("/probe", headers={HEADER: "trace-xyz"})
        assert resp.headers[HEADER] == "trace-xyz"

    def test_echoes_generated_uuid(self) -> None:
        with TestClient(_app_returning_state_request_id()) as client:
            resp = client.get("/probe")
        # The header MUST match what state.request_id said — same value.
        assert resp.headers[HEADER] == resp.json()["request_id"]

    def test_sanitizes_inbound_newline_injection(self) -> None:
        """The most-likely log-injection vector: \\n in the value.

        Whatever we use internally must be newline-free; that's what
        the logger / telemetry rows ingest.
        """
        with TestClient(_app_returning_state_request_id()) as client:
            resp = client.get("/probe", headers={HEADER: "abc\ninjected"})
        rid = resp.json()["request_id"]
        assert "\n" not in rid
        assert "abc" in rid
        assert "injected" in rid  # not lost, just scrubbed

    def test_request_id_for_returns_none_without_middleware(self) -> None:
        """Test contexts that bypass middleware shouldn't crash callers
        that ask for the request_id. None lets them fall back to a
        synthesized value."""
        app = FastAPI()  # NO request_id middleware

        @app.get("/probe")
        async def _probe(request: Request) -> dict[str, str | None]:
            return {"request_id": request_id_for(request)}

        with TestClient(app) as client:
            resp = client.get("/probe")
        assert resp.json()["request_id"] is None
