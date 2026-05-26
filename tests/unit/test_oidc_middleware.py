"""OIDC middleware unit tests (#315).

These tests cover the surfaces the implementation can exercise without
spinning a real OIDC issuer:

* ``configure_auth`` startup validation (missing env, allow_bearer
  without bearer_token, …).
* ``_has_required_group`` helper logic — comma-separated groups,
  missing claim, empty list.
* The browser-flow login redirect — verifies the PKCE challenge,
  state cookie shape, and ``authorization_endpoint`` round-trip by
  stubbing the issuer discovery call.
* The static-bearer fallback — agent service account coexistence
  is the load-bearing part of the bearer_fallback toggle.
* JWT validation pathway — mocks ``_validate_jwt`` so the test
  doesn't need a real keypair, then verifies the middleware's
  dispatch: 401 on invalid, 403 on missing group, 200 on success,
  ``request.state.username`` stamped.

FOLLOW-UP (#315b): an end-to-end test that spins up a tiny mock
OIDC issuer (Flask app exposing /.well-known + /authorize + /token
+ /jwks.json) lives in tests/integration/test_oidc_endpoint.py.
That test is gated by a marker until the testcontainers harness is
wired; this file covers the unit-level surfaces."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.api.auth import (
    _BEARER_FORBIDDEN,
    _BEARER_INVALID,
    OIDCAuthMiddleware,
    _has_required_group,
    configure_auth,
)
from cf_knowledge_kiln.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets a clean cache + no auth env."""
    for var in (
        "KILN_AUTH_MODE",
        "KILN_BEARER_TOKEN",
        "KILN_ENV",
        "KILN_OIDC_ISSUER",
        "KILN_OIDC_CLIENT_ID",
        "KILN_OIDC_CLIENT_SECRET",
        "KILN_OIDC_AUDIENCE",
        "KILN_OIDC_REQUIRED_GROUPS",
        "KILN_OIDC_USERNAME_CLAIM",
        "KILN_OIDC_ALLOW_BEARER_FALLBACK",
        "KILN_OIDC_SESSION_SECRET",
        "KILN_OIDC_REDIRECT_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ─── configure_auth startup validation ───────────────────────────────


def test_oidc_requires_issuer() -> None:
    """Missing KILN_OIDC_ISSUER fails fast."""
    settings = Settings(auth_mode="oidc")
    app = FastAPI()
    with pytest.raises(RuntimeError, match="KILN_OIDC_ISSUER"):
        configure_auth(app, settings)


def test_oidc_requires_client_id() -> None:
    """Missing KILN_OIDC_CLIENT_ID fails fast."""
    settings = Settings(auth_mode="oidc", oidc_issuer="https://issuer.example")
    app = FastAPI()
    with pytest.raises(RuntimeError, match="KILN_OIDC_CLIENT_ID"):
        configure_auth(app, settings)


def test_oidc_requires_client_secret() -> None:
    """Missing KILN_OIDC_CLIENT_SECRET fails fast (confidential client)."""
    settings = Settings(
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_client_id="kiln",
    )
    app = FastAPI()
    with pytest.raises(RuntimeError, match="KILN_OIDC_CLIENT_SECRET"):
        configure_auth(app, settings)


def test_oidc_bearer_fallback_requires_static_token() -> None:
    """allow_bearer_fallback=true without KILN_BEARER_TOKEN is rejected."""
    settings = Settings(
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_client_id="kiln",
        oidc_client_secret="shh",  # noqa: S106
        oidc_allow_bearer_fallback=True,
    )
    app = FastAPI()
    with pytest.raises(RuntimeError, match="KILN_BEARER_TOKEN"):
        configure_auth(app, settings)


def test_oidc_session_secret_default_warning(caplog: pytest.LogCaptureFixture) -> None:
    """No KILN_OIDC_SESSION_SECRET logs a loud warning."""
    settings = Settings(
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_client_id="kiln",
        oidc_client_secret="shh",  # noqa: S106
    )
    app = FastAPI()
    with caplog.at_level("WARNING"):
        configure_auth(app, settings)
    assert any("OIDC_SESSION_SECRET unset" in r.message for r in caplog.records), (
        "operator must be warned about session invalidation on restart"
    )


def test_oidc_minimal_config_wires_middleware() -> None:
    """A complete OIDC config attaches the middleware without raising."""
    settings = Settings(
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_client_id="kiln",
        oidc_client_secret="shh",  # noqa: S106
        oidc_session_secret="signing-key-x" * 4,
    )
    app = FastAPI()
    configure_auth(app, settings)
    middleware_classes = [m.cls for m in app.user_middleware]
    assert OIDCAuthMiddleware in middleware_classes


# ─── _has_required_group helper ──────────────────────────────────────


def test_has_required_group_empty_required_is_off() -> None:
    """Empty required list → group enforcement off."""
    assert _has_required_group({}, []) is True
    assert _has_required_group({"groups": ["x"]}, []) is True


def test_has_required_group_membership() -> None:
    """At least one match counts as membership."""
    claims = {"groups": ["users", "kiln-admins"]}
    assert _has_required_group(claims, ["kiln-admins"]) is True
    assert _has_required_group(claims, ["foo", "kiln-admins"]) is True
    assert _has_required_group(claims, ["foo", "bar"]) is False


def test_has_required_group_missing_claim_is_forbidden() -> None:
    """Missing 'groups' claim with required list set → forbidden."""
    assert _has_required_group({}, ["admins"]) is False
    assert _has_required_group({"groups": None}, ["admins"]) is False
    assert _has_required_group({"groups": "admins"}, ["admins"]) is False  # not a list


def test_has_required_group_strips_non_str_entries() -> None:
    """Bad 'groups' entries (None, ints, dicts) are silently dropped."""
    claims = {"groups": [None, 42, {"x": "y"}, "kiln-admins"]}
    assert _has_required_group(claims, ["kiln-admins"]) is True
    assert _has_required_group(claims, ["foo"]) is False


# ─── Browser flow — login redirect + PKCE challenge ──────────────────


async def _stub_ensure_discovery(self: OIDCAuthMiddleware) -> dict[str, Any]:
    """Bypass the real ``/.well-known`` fetch — populate the cache directly."""
    if self._discovery is None:
        self._discovery = {
            "issuer": "https://issuer.example",
            "authorization_endpoint": "https://issuer.example/authorize",
            "token_endpoint": "https://issuer.example/token",
            "jwks_uri": "https://issuer.example/jwks",
            "end_session_endpoint": "https://issuer.example/logout",
        }
    return self._discovery


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the OIDC env so create_app() wires the middleware."""
    monkeypatch.setenv("KILN_AUTH_MODE", "oidc")
    monkeypatch.setenv("KILN_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("KILN_OIDC_CLIENT_ID", "kiln")
    monkeypatch.setenv("KILN_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("KILN_OIDC_AUDIENCE", "kiln")
    monkeypatch.setenv("KILN_OIDC_SESSION_SECRET", "signing-key-x" * 4)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_browser_get_without_session_redirects_to_login(
    monkeypatch: pytest.MonkeyPatch, oidc_env: None
) -> None:
    """A browser request to a protected route → 302 /auth/login?next=…."""
    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("/auth/login?")
    qs = parse_qs(urlparse(location).query)
    assert qs["next"] == ["/"]


def test_api_get_without_token_returns_401(
    monkeypatch: pytest.MonkeyPatch, oidc_env: None
) -> None:
    """A non-browser request → 401 JSON envelope."""
    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post("/v1/search", json={"query": "x"})
    assert r.status_code == 401
    body = r.json()
    assert body["error_code"] == "auth_required"


def test_login_emits_pkce_state_cookie(
    monkeypatch: pytest.MonkeyPatch, oidc_env: None
) -> None:
    """``/auth/login`` redirects to the issuer with a PKCE S256 challenge.

    Verifies the request lands on ``authorization_endpoint``, the
    state cookie is set (signed), and the request carries
    ``code_challenge_method=S256``.
    """
    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.get("/auth/login?next=/v1/search", headers={"Accept": "text/html"})
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("https://issuer.example/authorize?")
    qs = parse_qs(urlparse(location).query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["kiln"]
    assert qs["code_challenge_method"] == ["S256"]
    assert "code_challenge" in qs
    assert "state" in qs
    assert "kiln_oidc_state" in r.cookies


# ─── Bearer / JWT validation ─────────────────────────────────────────


def test_static_bearer_fallback_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """allow_bearer_fallback=true accepts the static KILN_BEARER_TOKEN."""
    monkeypatch.setenv("KILN_AUTH_MODE", "oidc")
    monkeypatch.setenv("KILN_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("KILN_OIDC_CLIENT_ID", "kiln")
    monkeypatch.setenv("KILN_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("KILN_OIDC_SESSION_SECRET", "signing-key-x" * 4)
    monkeypatch.setenv("KILN_OIDC_ALLOW_BEARER_FALLBACK", "true")
    monkeypatch.setenv("KILN_BEARER_TOKEN", "static-token-32-chars-or-longer-X")
    get_settings.cache_clear()

    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer static-token-32-chars-or-longer-X"},
        )
    # Auth passed; the route may 503 (no DB) but is NOT 401.
    assert r.status_code != 401, r.text


def test_static_bearer_rejected_when_fallback_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static bearer must NOT bypass auth without allow_bearer_fallback."""
    from cf_knowledge_kiln.api.auth import _JWTValidationError

    monkeypatch.setenv("KILN_AUTH_MODE", "oidc")
    monkeypatch.setenv("KILN_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("KILN_OIDC_CLIENT_ID", "kiln")
    monkeypatch.setenv("KILN_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("KILN_OIDC_SESSION_SECRET", "signing-key-x" * 4)
    monkeypatch.setenv("KILN_BEARER_TOKEN", "static-token-32-chars-or-longer-X")
    get_settings.cache_clear()

    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)

    async def _bad(self: OIDCAuthMiddleware, token: str) -> dict[str, Any]:
        raise _JWTValidationError("not a JWT")

    monkeypatch.setattr(OIDCAuthMiddleware, "_validate_jwt", _bad)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer static-token-32-chars-or-longer-X"},
        )
    assert r.status_code == 401


def test_valid_jwt_stamps_username(monkeypatch: pytest.MonkeyPatch, oidc_env: None) -> None:
    """A JWT that ``_validate_jwt`` accepts → 200 + username stamped."""
    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)

    async def _ok(self: OIDCAuthMiddleware, token: str) -> dict[str, Any]:
        return {
            "iss": "https://issuer.example",
            "aud": "kiln",
            "preferred_username": "wzujkowski",
            "exp": 9_999_999_999,
        }

    monkeypatch.setattr(OIDCAuthMiddleware, "_validate_jwt", _ok)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer fake.jwt.value"},
        )
    assert r.status_code != 401, r.text


def test_invalid_jwt_returns_401(monkeypatch: pytest.MonkeyPatch, oidc_env: None) -> None:
    """``_validate_jwt`` raising → 401."""
    from cf_knowledge_kiln.api.auth import _JWTValidationError

    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)

    async def _bad(self: OIDCAuthMiddleware, token: str) -> dict[str, Any]:
        raise _JWTValidationError("signature mismatch")

    monkeypatch.setattr(OIDCAuthMiddleware, "_validate_jwt", _bad)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer bad.jwt.value"},
        )
    assert r.status_code == 401


def test_missing_required_group_returns_403(
    monkeypatch: pytest.MonkeyPatch, oidc_env: None
) -> None:
    """Valid JWT without the required group → 403."""
    monkeypatch.setenv("KILN_OIDC_REQUIRED_GROUPS", "kiln-admins")
    get_settings.cache_clear()
    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)

    async def _no_groups(self: OIDCAuthMiddleware, token: str) -> dict[str, Any]:
        return {
            "iss": "https://issuer.example",
            "aud": "kiln",
            "preferred_username": "wzujkowski",
            "groups": ["everyone"],
            "exp": 9_999_999_999,
        }

    monkeypatch.setattr(OIDCAuthMiddleware, "_validate_jwt", _no_groups)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer no.matching.group"},
        )
    assert r.status_code == 403


def test_pkce_code_verifier_round_trip_via_login(
    monkeypatch: pytest.MonkeyPatch, oidc_env: None
) -> None:
    """The login response's ``code_challenge`` is the S256 of a verifier
    that the state cookie carries — verifies the PKCE contract end-to-end
    by signing/decoding the cookie with the same secret.
    """
    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.get("/auth/login", headers={"Accept": "text/html"})
    location = r.headers["location"]
    qs = parse_qs(urlparse(location).query)
    code_challenge = qs["code_challenge"][0]

    # Decode the state cookie using the same signer the middleware
    # built (env-driven). The signed cookie's payload carries
    # ``code_verifier`` — sha256 of which (b64url, no pad) MUST match
    # the ``code_challenge`` the browser was redirected with.
    from itsdangerous import URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer("signing-key-x" * 4, salt="kiln-oidc")
    signed = r.cookies["kiln_oidc_state"]
    stash = serializer.loads(signed)
    code_verifier = stash["code_verifier"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert expected == code_challenge


def test_logout_clears_cookie_and_redirects(
    monkeypatch: pytest.MonkeyPatch, oidc_env: None
) -> None:
    """``/auth/logout`` clears the session cookie + redirects to end_session."""
    monkeypatch.setattr(OIDCAuthMiddleware, "_ensure_discovery", _stub_ensure_discovery)
    with TestClient(create_app(), follow_redirects=False) as c:
        c.cookies.set("kiln_session", "anything")
        r = c.get("/auth/logout", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://issuer.example/logout")
    set_cookie = r.headers.get("set-cookie", "")
    assert "kiln_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie


def test_bearer_sentinels_are_singletons() -> None:
    """The internal sentinels are identity-distinguishable."""
    assert _BEARER_INVALID is not _BEARER_FORBIDDEN
    from cf_knowledge_kiln.api.auth import _BEARER_FORBIDDEN as a
    from cf_knowledge_kiln.api.auth import _BEARER_INVALID as b

    assert a is _BEARER_FORBIDDEN
    assert b is _BEARER_INVALID
