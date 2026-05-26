"""Authentication middleware (Phase 8 — issues #29, #315).

Wired into :func:`create_app` based on ``KILN_AUTH_MODE``:

* ``none`` (default) — no auth. Refused when ``KILN_ENV=production``
  so a public-routable instance can't ship with the API wide open.
* ``bearer`` — every non-public request must carry
  ``Authorization: Bearer <token>``; compared in constant time to
  ``KILN_BEARER_TOKEN`` (env-only).
* ``mtls`` — deferred to a follow-up PR. ``ValueError`` at startup
  so an operator who sets it today gets a clear failure instead of
  silent acceptance.
* ``oidc`` (#315) — browser SSO via authorization-code + PKCE; API
  callers send a JWT obtained from the same issuer. ``aud``, ``iss``,
  ``exp``, and (optionally) ``groups`` are validated. When
  ``KILN_OIDC_ALLOW_BEARER_FALLBACK=true`` an inbound bearer header
  may carry the static ``bearer_token`` instead, so a service-account
  agent can coexist with browser users.

Public paths that bypass auth regardless of mode:

* ``/healthz``, ``/readyz``, ``/version`` — load balancer + ops probes.
* ``/static/*`` — UI assets. Not secrets.
* ``/openapi.json``, ``/docs``, ``/redoc`` — schema discovery. The
  schema doesn't expose data; protecting it would block the
  ``ultrareview``-style tooling that introspects the OpenAPI contract.
* The OIDC handshake paths (``/auth/login``, ``/auth/logout``,
  ``KILN_OIDC_REDIRECT_PATH``) — must be reachable unauthenticated;
  that's the whole point of the redirect handshake.

Per AGENTS.md: "No model-mediated authorization. Auth lives in
middleware, not in prompts."
"""

from __future__ import annotations

import base64
import hashlib
import logging
import posixpath
import secrets
import time
from secrets import compare_digest
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
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

# Session cookie name and TTL. 8 hours is a reasonable workday and
# matches the default Authentik session lifetime — operators on a
# tighter rotation cadence can clear the cookie via /auth/logout.
_SESSION_COOKIE = "kiln_session"
_SESSION_TTL_SECONDS = 8 * 3600

# State / PKCE cookie used to round-trip the auth handshake. Short
# TTL (10 minutes) because the user is bouncing through the IdP login
# screen — anything longer is a tracking surface, not a session.
_STATE_COOKIE = "kiln_oidc_state"
_STATE_TTL_SECONDS = 600


def configure_auth(app: FastAPI, settings: Settings) -> None:
    """Validate the auth config and attach middleware to ``app``.

    Raises ``RuntimeError`` at startup if the configuration is
    obviously wrong:

    * ``none`` mode while ``env=production`` — operator must opt in
      explicitly to running unauthenticated in prod.
    * ``bearer`` mode with no ``KILN_BEARER_TOKEN`` — would silently
      accept any Bearer header.
    * ``mtls`` mode — deferred until the follow-up PR lands.
    * ``oidc`` mode without issuer/client_id/client_secret — would
      crash at first request; surface the misconfig at startup.
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
    if mode == "oidc":
        _configure_oidc(app, settings)
        return
    # Settings.auth_mode is a Literal — Pydantic catches bad values
    # before this point. Defense in depth:
    raise RuntimeError(f"unknown KILN_AUTH_MODE={mode!r}")


def _configure_oidc(app: FastAPI, settings: Settings) -> None:
    """Validate OIDC config + attach :class:`OIDCAuthMiddleware`.

    Issuer discovery (the ``/.well-known/openid-configuration`` fetch)
    is deferred to first request — the middleware caches it on the
    instance. Operators who want a fail-fast startup probe can curl
    the issuer URL from the deploy pipeline before ``cf push``.
    """
    if not settings.oidc_issuer:
        raise RuntimeError("KILN_AUTH_MODE=oidc requires KILN_OIDC_ISSUER to be set.")
    if not settings.oidc_client_id:
        raise RuntimeError("KILN_AUTH_MODE=oidc requires KILN_OIDC_CLIENT_ID to be set.")
    # client_secret is required for confidential clients (the default
    # OIDC pattern for server-side apps). A public client (no secret)
    # is supportable in principle but kiln's HTMX UI is server-side
    # rendered — declaring this required removes a class of misconfig.
    if not settings.oidc_client_secret:
        raise RuntimeError(
            "KILN_AUTH_MODE=oidc requires KILN_OIDC_CLIENT_SECRET to be set. "
            "Public clients without a secret are a separate follow-up."
        )
    # Session-cookie signing key. Default to a runtime-generated value
    # with a loud warning so operators discover the multi-instance
    # invalidation trap before they hit it in production.
    session_secret = settings.oidc_session_secret
    if not session_secret:
        session_secret = secrets.token_urlsafe(32)
        logger.warning(
            "auth: KILN_OIDC_SESSION_SECRET unset — generated a random key for this "
            "process. Set KILN_OIDC_SESSION_SECRET to a stable shared secret in "
            "production or sessions will invalidate on every restart / reschedule."
        )
    # Fallback bearer requires the static token to actually exist;
    # otherwise the fallback path is unreachable and the operator's
    # service-account integration silently 401s.
    if settings.oidc_allow_bearer_fallback and not settings.bearer_token:
        raise RuntimeError(
            "KILN_OIDC_ALLOW_BEARER_FALLBACK=true requires KILN_BEARER_TOKEN to be set "
            "(the static token a service-account agent presents)."
        )
    app.add_middleware(
        OIDCAuthMiddleware,
        settings=settings,
        session_secret=session_secret,
    )
    logger.info(
        "auth: KILN_AUTH_MODE=oidc wired (issuer=%s, client_id=%s, bearer_fallback=%s, "
        "required_groups=%s)",
        settings.oidc_issuer,
        settings.oidc_client_id,
        settings.oidc_allow_bearer_fallback,
        settings.oidc_required_groups_list or "(none)",
    )


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


def _forbidden(
    *,
    message: str = "Forbidden.",
    request_id: str | None = None,
) -> Response:
    """403 response — used for OIDC group-membership failures (#315).

    Reuses ``error_code='auth_required'`` (the ``_STATUS_DEFAULTS`` map
    in api/errors.py already maps 403 to that code) — agents retry the
    same way as 401 (re-auth with broader scopes / different identity),
    so the same machine code is correct here. The 403 status + the
    ``message`` distinguish the case for humans.
    """
    from cf_knowledge_kiln.api.errors import ErrorResponse
    from cf_knowledge_kiln.api.request_id import HEADER as REQUEST_ID_HEADER

    envelope = ErrorResponse(
        error_code="auth_required",
        message=message,
        retry_safe=False,
        request_id=request_id,
    )
    headers: dict[str, str] = {}
    if request_id is not None:
        headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        envelope.model_dump(exclude_none=False),
        status_code=403,
        headers=headers,
    )


# ─── OIDC (#315) ────────────────────────────────────────────────────


def username_for(request: Request) -> str | None:
    """Return the authenticated username for ``request``, or ``None``.

    Set by :class:`OIDCAuthMiddleware` from the configured username
    claim (default ``preferred_username``). ``None`` when the request
    didn't carry an identity — this is the contract for the
    static-bearer / none modes, and for tests that bypass the
    middleware. Callers persist it on telemetry rows.
    """
    return getattr(request.state, "username", None)


class OIDCAuthMiddleware:
    """ASGI middleware enforcing OIDC SSO for browsers + JWT bearer for APIs.

    Browser flow (Accept: text/html, no Authorization header):

    1. GET ``/auth/login`` (or any protected GET without a session
       cookie): redirect to ``${authorization_endpoint}?response_type=
       code&client_id=...&redirect_uri=...&scope=openid+profile+email&
       state=...&code_challenge=...&code_challenge_method=S256``. The
       ``state`` value + the PKCE ``code_verifier`` are stashed in a
       short-lived signed cookie so the callback can validate them.
    2. ``${redirect_path}`` (the IdP redirects here): exchange the
       code for tokens against ``${token_endpoint}``, validate the
       ID token, set the kiln session cookie, redirect to the
       original ``next`` destination.
    3. ``/auth/logout``: clear the session cookie, redirect to
       ``${end_session_endpoint}`` when the IdP exposes one.

    API flow (any request with ``Authorization: Bearer <token>``):

    * Validate the JWT signature against the cached JWKS.
    * Enforce ``iss``, ``aud`` (defaults to client_id), ``exp``.
    * Optional group enforcement against ``oidc_required_groups``.
    * On success: stamp ``request.state.username`` from the
      configured claim and forward to the app.

    Fallback (when ``oidc_allow_bearer_fallback=true``): if the inbound
    bearer header matches ``bearer_token`` via :func:`compare_digest`,
    the request is accepted with ``username=None`` (the static-bearer
    contract). Lets a service-account agent coexist with browser users.

    Issuer discovery + JWKS fetch are lazy — they happen on the first
    request rather than at startup so the lifespan doesn't depend on
    the IdP being reachable at process start.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        session_secret: str,
    ) -> None:
        self._app = app
        self._settings = settings
        self._session_secret = session_secret
        # Lazy-initialized on first request.
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        # itsdangerous serializer used to sign session + state cookies.
        # Import is lazy so non-OIDC deployments don't need the dep.
        from itsdangerous import URLSafeTimedSerializer

        self._serializer = URLSafeTimedSerializer(session_secret, salt="kiln-oidc")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        path = request.url.path

        # Always-public paths (probes, openapi, static).
        if _is_public(path):
            await self._app(scope, receive, send)
            return

        # OIDC handshake endpoints are public — they're how the
        # session gets minted in the first place.
        if path == "/auth/login":
            response = await self._handle_login(request)
            await response(scope, receive, send)
            return
        if path == self._settings.oidc_redirect_path:
            response = await self._handle_callback(request)
            await response(scope, receive, send)
            return
        if path == "/auth/logout":
            response = await self._handle_logout(request)
            await response(scope, receive, send)
            return

        # Inbound bearer takes precedence over the cookie. Service
        # accounts always carry an Authorization header.
        authorization = request.headers.get("authorization")
        if authorization:
            username = await self._validate_bearer(authorization)
            if username is _BEARER_INVALID:
                response = _unauthorized(_request_id(request))
                await response(scope, receive, send)
                return
            if username is _BEARER_FORBIDDEN:
                response = _forbidden(
                    message="Required group membership not satisfied.",
                    request_id=_request_id(request),
                )
                await response(scope, receive, send)
                return
            # Username may be None (static-bearer fallback) or a string.
            request.state.username = username
            await self._app(scope, receive, send)
            return

        # No Authorization header — browser flow. Check the session.
        username = self._read_session(request)
        if username is not None:
            request.state.username = username
            await self._app(scope, receive, send)
            return

        # No identity. Browser-shaped requests get a 302 to login;
        # API-shaped requests get a 401.
        if _is_browser(request):
            response = self._redirect_to_login(request)
            await response(scope, receive, send)
            return
        response = _unauthorized(_request_id(request))
        await response(scope, receive, send)

    # ─── discovery / JWKS ────────────────────────────────────────────

    async def _ensure_discovery(self) -> dict[str, Any]:
        """Lazy-load + cache ``/.well-known/openid-configuration``."""
        if self._discovery is not None:
            return self._discovery
        import httpx

        issuer = (self._settings.oidc_issuer or "").rstrip("/")
        url = f"{issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            self._discovery = resp.json()
        logger.info(
            "auth: OIDC discovery loaded (auth=%s, token=%s, end_session=%s)",
            self._discovery.get("authorization_endpoint"),
            self._discovery.get("token_endpoint"),
            self._discovery.get("end_session_endpoint", "(none)"),
        )
        return self._discovery

    async def _ensure_jwks(self) -> dict[str, Any]:
        """Lazy-load + cache the issuer's JWKS."""
        if self._jwks is not None:
            return self._jwks
        discovery = await self._ensure_discovery()
        jwks_uri = discovery.get("jwks_uri")
        if not jwks_uri:
            raise RuntimeError("OIDC discovery document has no jwks_uri")
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            self._jwks = resp.json()
        return self._jwks

    # ─── bearer / JWT validation ─────────────────────────────────────

    async def _validate_bearer(self, authorization: str) -> Any:
        """Validate an inbound Authorization header.

        Returns:

        * ``str`` — the validated username from the configured claim.
        * ``None`` — static-bearer fallback accepted (no user identity).
        * ``""`` — JWT validated but missing the configured username
          claim (telemetry will record NULL).
        * :data:`_BEARER_INVALID` — header malformed, JWT signature /
          expiry / audience / issuer invalid.
        * :data:`_BEARER_FORBIDDEN` — JWT valid but missing required
          group membership.
        """
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return _BEARER_INVALID

        # Static-bearer fallback first — cheap, no network call.
        if (
            self._settings.oidc_allow_bearer_fallback
            and self._settings.bearer_token
            and compare_digest(
                value.encode("utf-8"),
                self._settings.bearer_token.encode("utf-8"),
            )
        ):
            # Service account — no user identity attached.
            return None

        # JWT path.
        try:
            claims = await self._validate_jwt(value)
        except _JWTValidationError as exc:
            logger.info("auth: oidc bearer JWT rejected: %s", exc)
            return _BEARER_INVALID

        if not _has_required_group(claims, self._settings.oidc_required_groups_list):
            return _BEARER_FORBIDDEN

        username = claims.get(self._settings.oidc_username_claim)
        if not isinstance(username, str) or not username:
            # Token validated but doesn't carry the configured claim —
            # treat as no identity rather than 401, so a misconfigured
            # IdP doesn't lock everyone out. Telemetry records NULL.
            return ""
        return username

    async def _validate_jwt(self, token: str) -> dict[str, Any]:
        """Validate ``token`` against the cached JWKS + issuer config.

        Raises :class:`_JWTValidationError` on any failure so the caller
        can render a single 401 envelope.
        """
        from authlib.jose import JsonWebKey, JsonWebToken
        from authlib.jose.errors import JoseError

        jwks = await self._ensure_jwks()
        try:
            key_set = JsonWebKey.import_key_set(jwks)
        except Exception as exc:  # pragma: no cover - defensive
            raise _JWTValidationError(f"JWKS import failed: {exc}") from exc
        # Restrict algorithms — never trust the alg declared in the
        # token's header alone. RS256 is the IdP default; ES256 covers
        # ECDSA-signing IdPs. HMAC algs are excluded so a misconfigured
        # IdP can't downgrade signature trust to a shared secret.
        jwt = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"])
        try:
            claims = jwt.decode(token, key_set)
        except JoseError as exc:
            raise _JWTValidationError(f"signature/decode: {exc}") from exc

        # Enforce iss + aud + exp explicitly. authlib's validate()
        # honors a claims_options dict.
        expected_audience = self._settings.oidc_audience or self._settings.oidc_client_id
        claims.options = {
            "iss": {
                "essential": True,
                "value": (self._settings.oidc_issuer or "").rstrip("/"),
            },
            "aud": {"essential": True, "value": expected_audience},
            "exp": {"essential": True},
        }
        try:
            claims.validate(now=int(time.time()), leeway=30)
        except JoseError as exc:
            raise _JWTValidationError(f"claim validation: {exc}") from exc
        return dict(claims)

    # ─── browser handshake ───────────────────────────────────────────

    async def _handle_login(self, request: Request) -> Response:
        """Begin the OIDC authorization-code + PKCE flow."""
        discovery = await self._ensure_discovery()
        auth_endpoint = discovery.get("authorization_endpoint")
        if not auth_endpoint:
            return _unauthorized(_request_id(request))

        next_url = request.query_params.get("next") or "/"
        state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        # RFC 7636 §4.2 — S256 challenge.
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )

        redirect_uri = _absolute_redirect_uri(request, self._settings.oidc_redirect_path)
        params = {
            "response_type": "code",
            "client_id": self._settings.oidc_client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        url = f"{auth_endpoint}?{urlencode(params)}"
        response = RedirectResponse(url=url, status_code=302)
        # Stash state + verifier + next_url in a signed cookie so the
        # callback can validate. Short-lived (10 min) — this is a
        # handshake cookie, not a session.
        signed = self._serializer.dumps(
            {"state": state, "code_verifier": code_verifier, "next": next_url}
        )
        response.set_cookie(
            _STATE_COOKIE,
            signed,
            max_age=_STATE_TTL_SECONDS,
            httponly=True,
            secure=_cookie_secure(request),
            samesite="lax",
            path="/",
        )
        return response

    async def _handle_callback(self, request: Request) -> Response:
        """Exchange code → tokens, validate, set session cookie."""
        from itsdangerous import BadSignature, SignatureExpired

        signed = request.cookies.get(_STATE_COOKIE)
        if not signed:
            logger.info("auth: oidc callback without state cookie")
            return _unauthorized(_request_id(request))
        try:
            stash = self._serializer.loads(signed, max_age=_STATE_TTL_SECONDS)
        except SignatureExpired:
            logger.info("auth: oidc state cookie expired")
            return _unauthorized(_request_id(request))
        except BadSignature:
            logger.warning("auth: oidc state cookie signature invalid")
            return _unauthorized(_request_id(request))

        inbound_state = request.query_params.get("state")
        if not inbound_state or not compare_digest(
            inbound_state.encode("utf-8"), stash["state"].encode("utf-8")
        ):
            logger.info("auth: oidc state mismatch")
            return _unauthorized(_request_id(request))

        code = request.query_params.get("code")
        if not code:
            logger.info("auth: oidc callback missing code")
            return _unauthorized(_request_id(request))

        # Exchange code → tokens.
        discovery = await self._ensure_discovery()
        token_endpoint = discovery.get("token_endpoint")
        if not token_endpoint:
            return _unauthorized(_request_id(request))
        redirect_uri = _absolute_redirect_uri(request, self._settings.oidc_redirect_path)
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self._settings.oidc_client_id,
                    "client_secret": self._settings.oidc_client_secret,
                    "code_verifier": stash["code_verifier"],
                },
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            logger.info(
                "auth: oidc token exchange failed (status=%d body=%s)",
                resp.status_code,
                resp.text[:200],
            )
            return _unauthorized(_request_id(request))
        token_response = resp.json()
        id_token = token_response.get("id_token")
        if not id_token:
            logger.info("auth: oidc token response missing id_token")
            return _unauthorized(_request_id(request))
        try:
            claims = await self._validate_jwt(id_token)
        except _JWTValidationError as exc:
            logger.info("auth: oidc id_token rejected: %s", exc)
            return _unauthorized(_request_id(request))

        if not _has_required_group(claims, self._settings.oidc_required_groups_list):
            return _forbidden(
                message="Required group membership not satisfied.",
                request_id=_request_id(request),
            )

        username_raw = claims.get(self._settings.oidc_username_claim)
        username = username_raw if isinstance(username_raw, str) and username_raw else ""

        # Set session cookie + redirect to original destination.
        next_url = stash.get("next") or "/"
        # Refuse open-redirect — only allow same-host relative paths.
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"
        response = RedirectResponse(url=next_url, status_code=302)
        signed_session = self._serializer.dumps({"u": username, "iat": int(time.time())})
        response.set_cookie(
            _SESSION_COOKIE,
            signed_session,
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            secure=_cookie_secure(request),
            samesite="lax",
            path="/",
        )
        response.delete_cookie(_STATE_COOKIE, path="/")
        return response

    async def _handle_logout(self, request: Request) -> Response:
        """Clear the session cookie and redirect to the IdP logout (if any)."""
        discovery = await self._ensure_discovery()
        end_session = discovery.get("end_session_endpoint")
        url = end_session if end_session else "/"
        response = RedirectResponse(url=url, status_code=302)
        response.delete_cookie(_SESSION_COOKIE, path="/")
        return response

    # ─── session-cookie helpers ──────────────────────────────────────

    def _read_session(self, request: Request) -> str | None:
        """Validate the session cookie + return the username inside it."""
        from itsdangerous import BadSignature, SignatureExpired

        signed = request.cookies.get(_SESSION_COOKIE)
        if not signed:
            return None
        try:
            stash = self._serializer.loads(signed, max_age=_SESSION_TTL_SECONDS)
        except (BadSignature, SignatureExpired):
            return None
        username = stash.get("u")
        return username if isinstance(username, str) else None

    def _redirect_to_login(self, request: Request) -> Response:
        """302 a browser request to ``/auth/login?next=<original>``."""
        # Preserve the original path so the callback can land them
        # where they were trying to go. Use the relative path only —
        # the IdP gets the absolute redirect_uri, but the local
        # ``next`` stays relative so an attacker can't smuggle in a
        # cross-host URL via a crafted Host header.
        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        params = urlencode({"next": next_url})
        return RedirectResponse(url=f"/auth/login?{params}", status_code=302)


# Sentinels for the validate_bearer return shape — keeps None as a
# real "static-bearer accepted, no identity" signal while still
# allowing two distinct failure modes (invalid vs forbidden).
class _Sentinel:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<sentinel {self.name}>"


_BEARER_INVALID = _Sentinel("invalid")
_BEARER_FORBIDDEN = _Sentinel("forbidden")


class _JWTValidationError(Exception):
    """Raised when JWT validation fails for any reason (signature, exp, aud)."""


def _has_required_group(claims: dict[str, Any], required: list[str]) -> bool:
    """True iff at least one of ``required`` is in the token's ``groups`` claim.

    Empty ``required`` short-circuits to True — group enforcement is off.
    Missing ``groups`` claim with ``required`` set is a 403; the
    operator opted into enforcement and the IdP didn't deliver.
    """
    if not required:
        return True
    groups = claims.get("groups")
    if not isinstance(groups, list):
        return False
    group_set = {g for g in groups if isinstance(g, str)}
    return any(g in group_set for g in required)


def _is_browser(request: Request) -> bool:
    """Heuristic: True iff this request looks like a browser GET.

    Used to decide whether an unauthenticated request gets a 302 to
    the login screen (browser) or a 401 JSON envelope (API). The
    Accept header is the reliable signal — htmx + standard browsers
    send ``Accept: text/html``; ``httpx`` / ``requests`` default to
    ``*/*`` or ``application/json``.
    """
    if request.method != "GET":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _absolute_redirect_uri(request: Request, path: str) -> str:
    """Compute the absolute ``redirect_uri`` for the OIDC handshake.

    Honors ``X-Forwarded-Proto`` + ``X-Forwarded-Host`` so the value
    is correct when kiln runs behind Caddy / gorouter.
    """
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    return f"{proto}://{host}{path}"


def _cookie_secure(request: Request) -> bool:
    """True iff this request reached us over HTTPS (directly or via XFP)."""
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return request.url.scheme == "https"


def _request_id(request: Request) -> str | None:
    """Shim for request_id_for — lazy-imported to avoid a cycle."""
    from cf_knowledge_kiln.api.request_id import request_id_for

    return request_id_for(request)


__all__ = ["OIDCAuthMiddleware", "configure_auth", "username_for"]
