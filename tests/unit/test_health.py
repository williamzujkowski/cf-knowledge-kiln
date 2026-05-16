"""Liveness, readiness, and version endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cf_knowledge_kiln import __version__


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "service": "cf-knowledge-kiln"}


def test_readyz_includes_postgres_check(client: TestClient) -> None:
    """Phase 2: readiness must surface the Postgres check."""
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert "postgres" in body["checks"]
    assert body["checks"]["postgres"] in {"ok", "failing"}
    assert body["status"] in {"ready", "degraded"}


def test_version_returns_package_version(client: TestClient) -> None:
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"version": __version__}


def test_openapi_spec_exposes_health_routes(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = spec["paths"]
    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/version" in paths
