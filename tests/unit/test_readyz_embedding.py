"""Tests for /readyz embedding-provider health reporting (#176).

The embedding provider is probed once at lifespan startup; /readyz
reports the cached result. These tests patch `build_provider_from_settings`
so no real model is loaded. Postgres is mocked healthy throughout so
the embedding check is the variable under test.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.db import connection as db_mod
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider

# `cf_knowledge_kiln.api.__init__` re-exports a FastAPI instance named
# `app`, which shadows the `app` submodule for every attribute-based
# lookup (`import ... as`, monkeypatch's dotted-string form, etc.).
# `sys.modules` is unaffected by that shadowing, so grab the genuine
# module object there to patch `build_provider_from_settings` — which
# `app.py` imported into its own namespace.
_APP_MODULE = sys.modules["cf_knowledge_kiln.api.app"]


@pytest.fixture(autouse=True)
def _healthy_db_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate env + mock Postgres healthy so embedding is the variable."""
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    monkeypatch.setenv(
        "KILN_DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/d"
    )  # pragma: allowlist secret
    monkeypatch.setattr(db_mod.Database, "ping", AsyncMock(return_value=True))
    monkeypatch.setattr(db_mod.Database, "dispose", AsyncMock())
    yield


class _BrokenProvider:
    """An embedding provider whose backend is unreachable."""

    provider = "broken"
    model = "broken"
    dimensions = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding backend unreachable")

    async def aclose(self) -> None:
        return None


def test_readyz_embedding_ok_when_probe_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured, working provider → embedding: ok, status ready."""
    monkeypatch.setattr(
        _APP_MODULE, "build_provider_from_settings", lambda _s: MockEmbeddingProvider()
    )
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["embedding"] == "ok"
    assert body["status"] == "ready"


def test_readyz_embedding_failing_when_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured-but-broken provider → embedding: failing → 503.

    This is the #176 scenario: a URL typo builds a provider object that
    only fails when actually used. The startup probe surfaces it.
    """
    monkeypatch.setattr(_APP_MODULE, "build_provider_from_settings", lambda _s: _BrokenProvider())
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["embedding"] == "failing"
    assert body["status"] == "degraded"


def test_readyz_embedding_not_configured_when_no_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No embedding config → not_configured. FTS-only is a valid mode,
    so it is surfaced but does NOT degrade readiness."""
    monkeypatch.setattr(_APP_MODULE, "build_provider_from_settings", lambda _s: None)
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["embedding"] == "not_configured"
    assert body["status"] == "ready"
