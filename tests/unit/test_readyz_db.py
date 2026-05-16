"""Tests for /readyz Postgres health reporting (no real DB).

The ``Database.ping`` method is patched. The integration test in
``tests/integration/`` exercises the real engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.db import connection as db_mod


@pytest.fixture(autouse=True)
def _isolated_db_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip env vars that would otherwise leak between cases."""
    monkeypatch.delenv("KILN_DATABASE_URL", raising=False)
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    yield


def test_readyz_reports_postgres_failing_when_no_db_configured() -> None:
    """No URL, no VCAP binding → postgres unavailable, status degraded."""
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["postgres"] == "failing"
    assert body["status"] == "degraded"


def test_readyz_reports_postgres_ok_when_ping_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KILN_DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/d"
    )  # pragma: allowlist secret
    monkeypatch.setattr(db_mod.Database, "ping", AsyncMock(return_value=True))
    monkeypatch.setattr(db_mod.Database, "dispose", AsyncMock())
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["postgres"] == "ok"
    assert body["status"] == "ready"


def test_readyz_reports_postgres_failing_when_ping_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KILN_DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/d"
    )  # pragma: allowlist secret
    monkeypatch.setattr(db_mod.Database, "ping", AsyncMock(return_value=False))
    monkeypatch.setattr(db_mod.Database, "dispose", AsyncMock())
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    body = response.json()
    assert body["checks"]["postgres"] == "failing"
    assert body["status"] == "degraded"
