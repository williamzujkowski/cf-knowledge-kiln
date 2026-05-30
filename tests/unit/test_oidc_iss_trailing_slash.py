"""Regression test for the OIDC ``iss`` trailing-slash mismatch (#392).

The existing OIDC middleware tests all monkeypatch ``_validate_jwt``, so
none exercise the *real* issuer-claim comparison. That gap let a bug ship:
``_validate_jwt`` built the expected issuer with ``rstrip("/")`` on the
configured ``KILN_OIDC_ISSUER``, while spec-compliant IdPs (e.g. Authentik)
advertise — and stamp into the id_token ``iss`` — an issuer URL that ends
in a trailing slash. authlib does exact-string equality, so every login
failed with ``invalid_claim: Invalid claim 'iss'``.

These tests drive the real ``_validate_jwt`` end to end with an RSA keypair
and a token whose ``iss`` ends in ``/``. The fix validates the token's
``iss`` against the discovery document's ``issuer`` verbatim, so a
trailing-slash issuer round-trips correctly.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from authlib.jose import JsonWebKey, jwt

from cf_knowledge_kiln.api.auth import (
    OIDCAuthMiddleware,
    _JWTValidationError,
)
from cf_knowledge_kiln.config import Settings

# Authentik-shaped issuer: per-provider URL WITH a trailing slash. This is
# the exact shape that broke before the fix.
ISSUER_WITH_SLASH = "https://auth.example/application/o/kiln/"
CLIENT_ID = "kiln-client-id"
KID = "test-key-1"

# One keypair for the module. The private key signs tokens; the public
# half is published in the stub JWKS. generate_key with options={"kid": …}
# stamps the kid into both halves (RSAKey is immutable — no item assignment).
_PRIVATE_JWK = JsonWebKey.generate_key("RSA", 2048, options={"kid": KID}, is_private=True)
_PUBLIC_JWK = _PRIVATE_JWK.as_dict(is_private=False)


async def _noop(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
    return None


def _make_middleware() -> OIDCAuthMiddleware:
    """Middleware with discovery + JWKS caches pre-seeded (no network).

    Discovery advertises the trailing-slash issuer verbatim, exactly as
    Authentik's ``/.well-known/openid-configuration`` does.
    """
    settings = Settings(
        auth_mode="oidc",
        oidc_issuer=ISSUER_WITH_SLASH,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="shh",  # pragma: allowlist secret
        oidc_session_secret="signing-key-x" * 4,  # pragma: allowlist secret
    )
    mw = OIDCAuthMiddleware(
        _noop,
        settings=settings,
        session_secret=settings.oidc_session_secret,
    )
    mw._discovery = {
        "issuer": ISSUER_WITH_SLASH,
        "authorization_endpoint": f"{ISSUER_WITH_SLASH}authorize",
        "token_endpoint": f"{ISSUER_WITH_SLASH}token",
        "jwks_uri": f"{ISSUER_WITH_SLASH}jwks",
    }
    mw._jwks = {"keys": [_PUBLIC_JWK]}
    return mw


def _mint_id_token(*, iss: str, aud: str) -> str:
    header = {"alg": "RS256", "kid": KID}
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": "user-123",
        "preferred_username": "alice",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    return jwt.encode(header, payload, _PRIVATE_JWK).decode("ascii")


async def test_validate_jwt_accepts_trailing_slash_issuer() -> None:
    """An id_token whose ``iss`` ends in ``/`` validates (regression #392).

    Pre-fix this raised ``invalid_claim: Invalid claim 'iss'`` because the
    expected issuer was ``rstrip("/")``-ed to ``…/kiln`` while the token
    carried ``…/kiln/``.
    """
    mw = _make_middleware()
    token = _mint_id_token(iss=ISSUER_WITH_SLASH, aud=CLIENT_ID)

    claims = await mw._validate_jwt(token)

    assert claims["iss"] == ISSUER_WITH_SLASH
    assert claims["preferred_username"] == "alice"


async def test_validate_jwt_rejects_wrong_issuer() -> None:
    """A token from a different issuer is still rejected (no over-loosening)."""
    mw = _make_middleware()
    token = _mint_id_token(iss="https://evil.example/", aud=CLIENT_ID)

    with pytest.raises(_JWTValidationError):
        await mw._validate_jwt(token)


async def test_validate_jwt_rejects_wrong_audience() -> None:
    """``aud`` enforcement is unaffected by the issuer fix."""
    mw = _make_middleware()
    token = _mint_id_token(iss=ISSUER_WITH_SLASH, aud="some-other-client")

    with pytest.raises(_JWTValidationError):
        await mw._validate_jwt(token)
