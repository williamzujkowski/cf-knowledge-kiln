"""OIDC settings unit tests (#315).

The OIDC settings surface is documented in the issue: seven env vars
(``KILN_OIDC_*``) plus an ``AuthMode = oidc`` literal on the existing
``KILN_AUTH_MODE``. These tests pin:

* env vars resolve into the expected attribute names + types
* ``oidc_required_groups`` parses into ``oidc_required_groups_list``
  correctly (trim whitespace, drop empties)
* The :data:`AuthMode` literal includes ``oidc`` so Pydantic doesn't
  reject the new value at load time

These tests don't build a FastAPI app — they're settings-shape only,
so the JWT/JWKS dependencies are not required.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from cf_knowledge_kiln.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets a clean cache + no KILN_OIDC_* env."""
    for var in (
        "KILN_AUTH_MODE",
        "KILN_BEARER_TOKEN",
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


def test_oidc_defaults_none() -> None:
    """Unset env: every OIDC setting is None / its declared default."""
    settings = Settings()
    assert settings.oidc_issuer is None
    assert settings.oidc_client_id is None
    assert settings.oidc_client_secret is None
    assert settings.oidc_audience is None
    assert settings.oidc_required_groups is None
    assert settings.oidc_username_claim == "preferred_username"
    assert settings.oidc_allow_bearer_fallback is False
    assert settings.oidc_session_secret is None
    assert settings.oidc_redirect_path == "/auth/callback"


def test_auth_mode_accepts_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KILN_AUTH_MODE=oidc`` is a valid Literal value."""
    monkeypatch.setenv("KILN_AUTH_MODE", "oidc")
    settings = Settings()
    assert settings.auth_mode == "oidc"


def test_auth_mode_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pydantic still rejects unknown auth modes."""
    monkeypatch.setenv("KILN_AUTH_MODE", "saml")
    # Pydantic v2 raises a ValidationError on enum-mismatch; check the
    # error class directly rather than blind Exception (B017).
    with pytest.raises(ValidationError):
        Settings()


def test_oidc_env_vars_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """All seven OIDC env vars round-trip via the KILN_ prefix."""
    monkeypatch.setenv("KILN_OIDC_ISSUER", "https://auth.example/application/o/kiln/")
    monkeypatch.setenv("KILN_OIDC_CLIENT_ID", "kiln")
    monkeypatch.setenv("KILN_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("KILN_OIDC_AUDIENCE", "kiln-aud")
    monkeypatch.setenv("KILN_OIDC_REQUIRED_GROUPS", "admins,kiln-users")
    monkeypatch.setenv("KILN_OIDC_USERNAME_CLAIM", "sub")
    monkeypatch.setenv("KILN_OIDC_ALLOW_BEARER_FALLBACK", "true")
    monkeypatch.setenv("KILN_OIDC_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("KILN_OIDC_REDIRECT_PATH", "/oidc/cb")
    settings = Settings()
    assert settings.oidc_issuer == "https://auth.example/application/o/kiln/"
    assert settings.oidc_client_id == "kiln"
    assert settings.oidc_client_secret == "shh"
    assert settings.oidc_audience == "kiln-aud"
    assert settings.oidc_required_groups == "admins,kiln-users"
    assert settings.oidc_username_claim == "sub"
    assert settings.oidc_allow_bearer_fallback is True
    assert settings.oidc_session_secret == "x" * 32
    assert settings.oidc_redirect_path == "/oidc/cb"


def test_required_groups_list_parses_clean() -> None:
    """``admins,kiln-users`` → ``['admins', 'kiln-users']``."""
    settings = Settings(oidc_required_groups="admins,kiln-users")
    assert settings.oidc_required_groups_list == ["admins", "kiln-users"]


def test_required_groups_list_trims_whitespace() -> None:
    """Operator-pasted whitespace + empties don't poison the list."""
    settings = Settings(oidc_required_groups="admins, , kiln-users  ,,")
    assert settings.oidc_required_groups_list == ["admins", "kiln-users"]


def test_required_groups_list_empty_when_unset() -> None:
    """Unset / empty → ``[]`` (group enforcement off)."""
    assert Settings().oidc_required_groups_list == []
    assert Settings(oidc_required_groups="").oidc_required_groups_list == []
    assert Settings(oidc_required_groups="   ,, ").oidc_required_groups_list == []


def test_username_claim_default_is_preferred_username() -> None:
    """Default username claim matches the IdP convention."""
    assert Settings().oidc_username_claim == "preferred_username"


def test_redirect_path_default_is_auth_callback() -> None:
    """Default redirect path is ``/auth/callback``."""
    assert Settings().oidc_redirect_path == "/auth/callback"
