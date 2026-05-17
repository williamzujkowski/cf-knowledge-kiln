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
from secrets import compare_digest

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from cf_knowledge_kiln.config import Settings

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
        if settings.env == "production":
            raise RuntimeError(
                "KILN_AUTH_MODE=none is refused when KILN_ENV=production. "
                "Set KILN_AUTH_MODE=bearer + KILN_BEARER_TOKEN, or set "
                "KILN_ENV to a non-production value if you really need this."
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
        app.add_middleware(_BearerAuthMiddleware, expected_token=token)
        logger.info("auth: KILN_AUTH_MODE=bearer wired (token length=%d)", len(token))
        return
    # Settings.auth_mode is a Literal — Pydantic catches bad values
    # before this point. Defense in depth:
    raise RuntimeError(f"unknown KILN_AUTH_MODE={mode!r}")


def _is_public(path: str) -> bool:
    """Return True iff this path bypasses auth in every mode."""
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


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
            response = _unauthorized()
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


def _unauthorized() -> Response:
    return JSONResponse(
        {"detail": "Authentication required."},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="kiln"'},
    )


__all__ = ["configure_auth"]
