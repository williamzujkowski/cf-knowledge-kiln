"""Bearer-token authentication middleware (Phase 8, issue #29).

Wired into :func:`create_app` based on ``KILN_AUTH_MODE``:

* ``none`` (default) — no auth. Refused when ``KILN_ENV=production``
  so a public-routable instance can't ship with the API wide open.
* ``bearer`` — every non-public request must carry
  ``Authorization: Bearer <token>``; compared in constant time to
  ``KILN_BEARER_TOKEN`` (env-only).
* ``mtls`` — deferred to a follow-up PR. ``ValueError`` at startup
  so an operator who sets it today gets a clear failure instead of
  silent acceptance.

Public paths that bypass auth regardless of mode:

* ``/healthz``, ``/readyz``, ``/version`` — load balancer + ops probes.
* ``/static/*`` — UI assets. Not secrets.
* ``/openapi.json``, ``/docs``, ``/redoc`` — schema discovery. The
  schema doesn't expose data; protecting it would block the
  ``ultrareview``-style tooling that introspects the OpenAPI contract.

Per AGENTS.md: "No model-mediated authorization. Auth lives in
middleware, not in prompts."
"""

from __future__ import annotations

import logging
import posixpath
from secrets import compare_digest

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from cf_knowledge_kiln.config import Settings

# Minimum bearer token length. 32 chars is roughly 192 bits of entropy
# for a base64 random token — enough headroom that brute-force is not
# the easy path. Operators who want a different threshold can override
# at deployment with their own token-generation policy; we just gate
# against trivially-short tokens like "x" or "test".
_MIN_BEARER_LENGTH: int = 32

# OpenAPI surface is public by design: `ultrareview` and similar
# introspection tooling depend on it, the contract is meant to be
# discoverable, and the schema doesn't expose data — only shapes.
# Operators who want to hide the surface can disable openapi_url in
# create_app() for their deployment.
#
# Schema discovery is NOT a "secret"; it's documentation.

logger = logging.getLogger(__name__)

# Paths that bypass auth in every mode.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/healthz", "/readyz", "/version", "/openapi.json", "/docs", "/redoc"}
)
_PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)


def configure_auth(app: FastAPI, settings: Settings) -> None:
    """Validate the auth config and attach middleware to ``app``.

    Raises ``RuntimeError`` at startup if the configuration is
    obviously wrong:

    * ``none`` mode while ``env=production`` — operator must opt in
      explicitly to running unauthenticated in prod.
    * ``bearer`` mode with no ``KILN_BEARER_TOKEN`` — would silently
      accept any Bearer header.
    * ``mtls`` mode — deferred until the follow-up PR lands.
    """
    mode = settings.auth_mode
    if mode == "none":
        # Refuse for staging AND production. Staging often mirrors
        # production data and is exposed to broader audiences than
        # dev; an unauthenticated staging instance is almost always
        # an operator mistake.
        if settings.env in ("production", "staging"):
            raise RuntimeError(
                f"KILN_AUTH_MODE=none is refused when KILN_ENV={settings.env}. "
                f"Set KILN_AUTH_MODE=bearer + KILN_BEARER_TOKEN, or set "
                f"KILN_ENV=development if you really need an open instance."
            )
        logger.warning(
            "auth: KILN_AUTH_MODE=none — every endpoint is unauthenticated. "
            "Only safe behind a trusted network boundary."
        )
        return
    if mode == "mtls":
        raise RuntimeError(
            "KILN_AUTH_MODE=mtls is declared but not yet implemented. "
            "Set bearer for now; mTLS lands in a follow-up to #29."
        )
    if mode == "bearer":
        token = settings.bearer_token
        if not token:
            raise RuntimeError("KILN_AUTH_MODE=bearer requires KILN_BEARER_TOKEN to be set.")
        if len(token) < _MIN_BEARER_LENGTH:
            raise RuntimeError(
                f"KILN_BEARER_TOKEN is too short ({len(token)} chars); "
                f"minimum is {_MIN_BEARER_LENGTH}. Generate one with "
                f'`python -c "import secrets; print(secrets.token_urlsafe(32))"`.'
            )
        app.add_middleware(_BearerAuthMiddleware, expected_token=token)
        logger.info("auth: KILN_AUTH_MODE=bearer wired (token length=%d)", len(token))
        return
    # Settings.auth_mode is a Literal — Pydantic catches bad values
    # before this point. Defense in depth:
    raise RuntimeError(f"unknown KILN_AUTH_MODE={mode!r}")


def _is_public(path: str) -> bool:
    """Return True iff this path bypasses auth in every mode.

    Normalizes ``..`` and ``//`` BEFORE matching so an attacker can't
    reach protected routes via ``/static/../v1/search`` — the raw
    request path passes the prefix check, then Starlette's router
    resolves the literal path against its routes, and any bypass-then-
    route mismatch silently leaks the bypass to the application's
    rear without ever surfacing as a 401.

    Defense in depth: refuse to bypass anything that still contains
    a ``..`` segment after normalization (would only happen for
    relative paths like ``../x``, which a well-formed HTTP request
    can't produce — but the cost of being wrong is full bypass).
    """
    normalized = posixpath.normpath(path or "/")
    if ".." in normalized.split("/"):
        return False
    if normalized in _PUBLIC_PATHS:
        return True
    return any(normalized.startswith(p) for p in _PUBLIC_PREFIXES)


class _BearerAuthMiddleware:
    """ASGI middleware that rejects requests without a valid bearer token.

    Implements the raw ASGI shape (rather than
    :class:`starlette.middleware.base.BaseHTTPMiddleware`) so it can
    short-circuit before any request body is read — important when
    rejecting POSTs with large payloads.
    """

    def __init__(self, app: ASGIApp, *, expected_token: str) -> None:
        self._app = app
        self._expected = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if _is_public(request.url.path):
            await self._app(scope, receive, send)
            return
        if not _authorized(request, self._expected):
            # #258: thread the request_id (set by the request_id
            # middleware running upstream of this auth wrapper) into
            # the structured envelope so an operator can correlate
            # the 401 with the per-request log line.
            from cf_knowledge_kiln.api.request_id import request_id_for

            response = _unauthorized(request_id_for(request))
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _authorized(request: Request, expected_token: str) -> bool:
    """True iff the request carries a valid ``Authorization: Bearer <expected>``.

    Uses :func:`secrets.compare_digest` for the equality check so the
    response time doesn't leak information about the prefix that
    matched.
    """
    header = request.headers.get("authorization")
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return compare_digest(value.encode("utf-8"), expected_token.encode("utf-8"))


def _unauthorized(request_id: str | None = None) -> Response:
    # #258: structured envelope. The auth middleware runs BEFORE the
    # global exception handler can intercept anything, so we build the
    # envelope inline rather than raising HTTPException. WWW-Authenticate
    # stays as the RFC-required hint for HTTP clients.
    from cf_knowledge_kiln.api.errors import ErrorResponse
    from cf_knowledge_kiln.api.request_id import HEADER as REQUEST_ID_HEADER

    envelope = ErrorResponse(
        error_code="auth_required",
        message="Authentication required.",
        retry_safe=False,
        request_id=request_id,
    )
    headers = {"WWW-Authenticate": 'Bearer realm="kiln"'}
    if request_id is not None:
        headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        envelope.model_dump(exclude_none=False),
        status_code=401,
        headers=headers,
    )


__all__ = ["configure_auth"]
