"""Tests for the per-request observability logging middleware (#178)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app

_LOGGER = "cf_knowledge_kiln.request"


def test_request_is_logged_with_method_path_status_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A normal request emits one structured log line."""
    with caplog.at_level(logging.INFO, logger=_LOGGER), TestClient(create_app()) as client:
        response = client.get("/version")
    assert response.status_code == 200

    lines = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    assert len(lines) == 1
    line = lines[0]
    assert "method=GET" in line
    assert "path=/version" in line
    assert "status=200" in line
    assert "duration_ms=" in line


def test_health_probe_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """`/healthz` is high-frequency, low-signal — skipped to keep logs clean."""
    with caplog.at_level(logging.INFO, logger=_LOGGER), TestClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert [r for r in caplog.records if r.name == _LOGGER] == []
