"""Verify the Phase 5 contract-surface stubs (#37 follow-up).

`/v1/search` and `/v1/agent/context-pack` are declared in the
hand-authored OpenAPI spec but not implemented until Phase 5. These
stubs return 501 so clients can test their 501-handling against the
real server and so the OpenAPI drift test
(`tests/unit/test_openapi_drift.py`) can compare paths without
needing schema parity for the not-yet-implemented bodies.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_v1_search_returns_501(client: TestClient) -> None:
    response = client.post("/v1/search", json={"query": "hello"})
    assert response.status_code == 501
    assert "Phase 5" in response.json()["detail"]


def test_v1_agent_context_pack_returns_501(client: TestClient) -> None:
    response = client.post("/v1/agent/context-pack", json={"task": "compose", "query": "hi"})
    assert response.status_code == 501
    assert "Phase 5" in response.json()["detail"]


def test_v1_search_returns_501_on_empty_body(client: TestClient) -> None:
    """An empty JSON body should still get the 501 — no pre-validation yet."""
    response = client.post("/v1/search", json={})
    assert response.status_code == 501


def test_v1_search_returns_501_when_body_omitted(client: TestClient) -> None:
    """POST with no body at all → still 501, not a 422 body-required error.

    The stubs deliberately accept any body (including none) so the
    501 contract is consistent regardless of what clients send while
    Phase 5 is in flight.
    """
    response = client.post("/v1/search")
    assert response.status_code == 501


def test_openapi_exposes_phase_5_routes(client: TestClient) -> None:
    """Both routes must appear in /openapi.json with the documented operationIds."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = spec["paths"]
    assert "/v1/search" in paths
    assert "/v1/agent/context-pack" in paths
    assert paths["/v1/search"]["post"]["operationId"] == "humanSearch"
    assert paths["/v1/agent/context-pack"]["post"]["operationId"] == "agentContextPack"
