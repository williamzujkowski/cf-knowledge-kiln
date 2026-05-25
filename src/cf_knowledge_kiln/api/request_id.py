"""Per-request correlation ID middleware (#260).

Generates or honors an ``X-Request-ID`` header on every incoming
request, exposes it on ``request.state.request_id``, and echoes it
on the response. Downstream code (logging, telemetry writes, error
responses) pulls the ID off ``request.state`` to thread the same
correlation key through every artifact a single request produces.

Why this matters: when an operator gets a user complaint quoting a
`context_pack_id` or `answer_id` (now persisted per #256), the
audit trail needs ONE key that joins the log line, the telemetry
row, the error envelope, and the agent's record. `X-Request-ID`
is that key.

Policy:

* If the inbound request carries an `X-Request-ID` header, honor it
  verbatim (after a light sanitization for log-safety — no newlines,
  bounded length).
* Otherwise generate a fresh UUID4.
* Always set the same value on `response.headers['X-Request-ID']`
  so the caller can record it in their own logs / share with
  support.

The middleware runs OUTSIDE the per-request log middleware (it's
installed first in ``create_app``, so the log middleware can pull
the ID off ``request.state`` when emitting its line).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

HEADER = "X-Request-ID"

# Inbound values are sanitized before they touch logs / telemetry.
# Allow alphanumeric + a small set of separators. The 200-char cap
# matches the OpenTelemetry W3C trace-id family (32 hex) plus some
# room for vendor prefixes.
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_LEN = 200


def _sanitize(value: str) -> str | None:
    """Trim + scrub an inbound X-Request-ID. ``None`` if unusable."""
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_LEN:
        value = value[:_MAX_LEN]
    # Replace anything outside the allowed set so a log scraper can't
    # be fooled by a newline-injected value.
    scrubbed = _SANITIZE_RE.sub("_", value)
    return scrubbed or None


def install_request_id_middleware(app: FastAPI) -> None:
    """Attach the X-Request-ID middleware to ``app``.

    Must be installed BEFORE any middleware that wants to read
    ``request.state.request_id`` (notably the per-request log
    middleware). FastAPI's middleware stack runs outermost-first,
    so the first-installed middleware is the LAST to wrap inbound —
    install_request_id_middleware therefore goes FIRST in create_app
    so it's the OUTERMOST layer and runs first inbound.
    """

    @app.middleware("http")
    async def _request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        inbound = request.headers.get(HEADER)
        request_id = _sanitize(inbound) if inbound else None
        if not request_id:
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        # Echo the (possibly-generated, possibly-sanitized) value so
        # the caller can record it. If a downstream middleware /
        # handler already set the header (unusual), we still wrote
        # state.request_id so logging sees the canonical value.
        response.headers[HEADER] = request_id
        return response


def request_id_for(request: Request) -> str | None:
    """Return the request-id stamped by the middleware, or None.

    Helper for handlers that don't want to reach into ``request.state``
    directly. None when the middleware hasn't installed (test
    contexts that bypass FastAPI's stack) so callers can fall back
    to ``str(uuid4())`` without crashing.
    """
    return getattr(request.state, "request_id", None)


__all__ = ["HEADER", "install_request_id_middleware", "request_id_for"]
