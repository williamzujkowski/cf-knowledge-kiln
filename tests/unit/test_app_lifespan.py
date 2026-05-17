"""FastAPI app lifespan wiring (#58).

The lifespan must build the embedding provider once at startup and
hand it back at shutdown. Phase 5 retrieval reads
``app.state.embedding_provider`` to embed user queries; without
this contract the API would either rebuild the provider per request
(unacceptable for the local sentence-transformers loader) or share
a long-lived instance with broken lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import get_settings
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider


@pytest.fixture
def models_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a minimal mock-provider config and point KILN at it."""
    path = tmp_path / "models.yaml"
    path.write_text(
        """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KILN_MODELS_CONFIG_PATH", str(path))
    get_settings.cache_clear()
    return path


def test_lifespan_attaches_embedding_provider_when_configured(
    models_config: Path,
) -> None:
    """A valid models.yaml at startup → app.state.embedding_provider is live."""
    with TestClient(create_app()) as client:
        provider = client.app.state.embedding_provider
        assert isinstance(provider, MockEmbeddingProvider)
        assert provider.dimensions == 768


def test_lifespan_attaches_rate_limiters(models_config: Path) -> None:
    """#79: both rate limiters are bound to app.state by the lifespan."""
    from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter

    with TestClient(create_app()) as client:
        assert isinstance(client.app.state.search_limiter, TokenBucketLimiter)
        assert isinstance(client.app.state.feedback_limiter, TokenBucketLimiter)


def test_lifespan_attaches_none_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No models.yaml → app.state.embedding_provider is None, app still starts."""
    monkeypatch.setenv("KILN_MODELS_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert client.app.state.embedding_provider is None
        # The app still serves health endpoints — degraded but up.
        assert client.get("/healthz").status_code == 200


def test_lifespan_refuses_to_start_on_malformed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad config → lifespan raises; app refuses to come up."""
    bad = tmp_path / "models.yaml"
    bad.write_text(
        """
models:
  embedding:
    provider: local
    name: Qwen/Qwen3-Embedding-8B   # excluded family
    dimensions: 1024
    enabled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KILN_MODELS_CONFIG_PATH", str(bad))
    get_settings.cache_clear()
    with pytest.raises(Exception, match="excluded"), TestClient(create_app()):
        pass
