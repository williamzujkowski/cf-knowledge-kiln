"""Pytest fixtures shared across the suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Drop the lru_cache around get_settings between tests.

    Without this, env-var mutations in one test leak into the next.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A fresh TestClient bound to a fresh app.

    Uses ``with`` so the FastAPI lifespan (DB engine startup / shutdown)
    actually runs. Tests that need a configured DB should set
    ``KILN_DATABASE_URL`` via ``monkeypatch.setenv`` before requesting
    this fixture.
    """
    with TestClient(create_app()) as c:
        yield c
