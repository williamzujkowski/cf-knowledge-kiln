"""Bearer-auth middleware tests (Phase 8 issue #29).

Drives the design via TDD. The middleware is wired into the FastAPI
app via :func:`create_app`; these tests mutate env vars + rebuild the
app, then hit it through :class:`TestClient`.

Acceptance per #29:

* ``none`` mode rejected in production (warn loudly, fail-start).
* Bearer mode rejects requests with no/invalid token.
* Auth happens BEFORE any retrieval or ingestion endpoint runs.
* Health (``/healthz``, ``/readyz``, ``/version``) MUST stay public.

mTLS is deferred to a follow-up PR.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import get_settings


@pytest.fixture(autouse=True)
def _clear(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets a clean settings cache + env."""
    for var in ("KILN_AUTH_MODE", "KILN_BEARER_TOKEN", "KILN_ENV"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client() -> TestClient:
    return TestClient(create_app())


# ─── Health endpoints are ALWAYS public ─────────────────────────────


def test_healthz_public_in_none_mode() -> None:
    with _client() as c:
        assert c.get("/healthz").status_code == 200


def test_healthz_public_in_bearer_mode_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN", "test-bearer-token-32-chars-or-longer"
    )  # pragma: allowlist secret
    get_settings.cache_clear()
    with _client() as c:
        # No Authorization header — must still get a 200 from health.
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code in (200, 503)  # depends on DB
        assert c.get("/version").status_code == 200


# ─── None mode ──────────────────────────────────────────────────────


def test_none_mode_allows_v1_search_without_token() -> None:
    """Default (development) mode: no auth enforced anywhere."""
    with _client() as c:
        # Hits the route but will 503 (no DB bound in this test). The
        # important thing is it gets PAST auth — i.e., not 401.
        r = c.post("/v1/search", json={"query": "x"})
        assert r.status_code != 401


def test_none_mode_rejected_when_env_is_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production + none mode must fail-start (#29 acceptance).

    create_app() must raise loudly so the operator can't ship a
    publicly-routable instance with no auth.
    """
    monkeypatch.setenv("KILN_AUTH_MODE", "none")
    monkeypatch.setenv("KILN_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="KILN_AUTH_MODE=none"):
        create_app()


# ─── Bearer mode ────────────────────────────────────────────────────


def test_bearer_mode_requires_token_to_be_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bearer mode with no KILN_BEARER_TOKEN must fail-start.

    Otherwise the operator silently shipped 'allow anything that
    LOOKS like a bearer header' which is worse than no auth.
    """
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.delenv("KILN_BEARER_TOKEN", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="KILN_BEARER_TOKEN"):
        create_app()


def test_bearer_mode_rejects_no_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN", "test-bearer-token-32-chars-or-longer"
    )  # pragma: allowlist secret
    get_settings.cache_clear()
    with _client() as c:
        r = c.post("/v1/search", json={"query": "x"})
        assert r.status_code == 401
        assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_bearer_mode_rejects_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN", "test-bearer-token-32-chars-or-longer"
    )  # pragma: allowlist secret
    get_settings.cache_clear()
    with _client() as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert r.status_code == 401


def test_bearer_mode_rejects_wrong_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization: Basic xxx must be rejected even with the right secret."""
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN", "test-bearer-token-32-chars-or-longer"
    )  # pragma: allowlist secret
    get_settings.cache_clear()
    with _client() as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Basic s3kret"},
        )
        assert r.status_code == 401


def test_bearer_mode_accepts_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "test-bearer-token-32-chars-or-longer"  # pragma: allowlist secret
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv("KILN_BEARER_TOKEN", token)
    get_settings.cache_clear()
    with _client() as c:
        r = c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Not 401 — auth passed. Will 503 because no DB is bound; that's fine.
        assert r.status_code != 401


def test_bearer_mode_html_routes_also_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /search HTMX route and the / page must also enforce bearer."""
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN", "test-bearer-token-32-chars-or-longer"
    )  # pragma: allowlist secret
    get_settings.cache_clear()
    with _client() as c:
        assert c.get("/").status_code == 401
        assert c.post("/search", data={"query": "x"}).status_code == 401


def test_bearer_mode_static_assets_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CSS + favicons under /static MUST stay public.

    Otherwise a browser that fetches the page (already 401'd) but
    then tries to load /static/kiln.css gets another 401 + the
    network panel fills with errors. Static assets aren't secrets.
    """
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN", "test-bearer-token-32-chars-or-longer"
    )  # pragma: allowlist secret
    get_settings.cache_clear()
    with _client() as c:
        assert c.get("/static/kiln.css").status_code == 200


def test_bearer_mode_rejects_path_traversal_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH (PR #77 review): /static/../v1/search must not bypass auth.

    The raw request path matches the /static/ public prefix, but
    Starlette's router resolves the literal path against its routes.
    Without normalization in _is_public, auth was skipped before the
    route mismatch resolved, leaking the bypass to anything that
    happened to match (or to a 405 like the original report showed).
    The middleware now normalizes the path before the public-prefix
    check.
    """
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    # 40-char token: meets the 32-char minimum
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN",
        "test-bearer-token-32-chars-or-longer",  # pragma: allowlist secret
    )
    get_settings.cache_clear()
    with _client() as c:
        for path in (
            "/static/../v1/search",
            "/static/..//v1/search",
            "/healthz/../v1/search",
            "/openapi.json/../v1/search",
        ):
            r = c.post(path, json={"query": "x"})
            assert r.status_code == 401, (
                f"{path!r} bypassed auth (got {r.status_code} not 401) — path-traversal regression."
            )


def test_none_mode_rejected_when_env_is_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM (PR #77 review): staging is also a "real" environment.

    Operators often mirror production data into staging and expose
    it to broader audiences than dev. An unauthenticated staging
    instance is almost always a deployment mistake — refuse it the
    same way we refuse production.
    """
    monkeypatch.setenv("KILN_AUTH_MODE", "none")
    monkeypatch.setenv("KILN_ENV", "staging")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="KILN_AUTH_MODE=none"):
        create_app()


def test_bearer_mode_rejects_token_shorter_than_32_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW (PR #77 review): refuse trivially short tokens at startup."""
    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv("KILN_BEARER_TOKEN", "short")  # pragma: allowlist secret
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="too short"):
        create_app()


def test_bearer_mode_uses_constant_time_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: the comparison must use secrets.compare_digest.

    We assert the implementation by patching compare_digest and
    confirming the middleware called it (instead of a `==`).
    """
    import secrets as secrets_mod
    from unittest.mock import patch

    monkeypatch.setenv("KILN_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "KILN_BEARER_TOKEN", "test-bearer-token-32-chars-or-longer"
    )  # pragma: allowlist secret
    get_settings.cache_clear()
    real_cd = secrets_mod.compare_digest
    calls: list[int] = []

    def counted(a: object, b: object) -> bool:
        calls.append(1)
        return real_cd(a, b)  # type: ignore[arg-type]

    with patch("cf_knowledge_kiln.api.auth.compare_digest", counted), _client() as c:
        c.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer s3kret"},  # pragma: allowlist secret
        )
    assert sum(calls) >= 1, "bearer middleware must use secrets.compare_digest"
