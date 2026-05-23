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


def test_lifespan_attaches_retrieval_config_and_phrases(models_config: Path) -> None:
    """#183: the lifespan parses config/security.yaml once into app.state."""
    from cf_knowledge_kiln.retrieval import RetrievalConfig

    with TestClient(create_app()) as client:
        assert isinstance(client.app.state.retrieval_config, RetrievalConfig)
        assert isinstance(client.app.state.prompt_injection_phrases, list)


def test_retrieval_config_loaded_once_not_per_request(
    models_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#183: config/security.yaml is parsed once at startup, never per request.

    Before #183 the retrieval dependencies re-read + re-parsed the file
    on every request. This wraps the loader with a call counter,
    confirms the lifespan invokes it exactly once, then resolves the
    `get_retrieval_config` dependency repeatedly and confirms the count
    does not move — the dependency reads `app.state`, not the file.

    `load_retrieval_config` is imported by value into BOTH `api.app`
    (the lifespan) and `api.dependencies` (the fallback / any future
    per-request regression). Both bindings are patched with the same
    counter so a partial regression — re-adding a per-request load via
    the dependencies binding — is caught, not silently missed.
    """
    import sys
    from types import SimpleNamespace

    from cf_knowledge_kiln.api.dependencies import get_retrieval_config

    app_module = sys.modules["cf_knowledge_kiln.api.app"]
    deps_module = sys.modules["cf_knowledge_kiln.api.dependencies"]
    real_loader = app_module.load_retrieval_config
    calls: list[int] = []

    def counting_loader(path: object) -> object:
        calls.append(1)
        return real_loader(path)

    monkeypatch.setattr(app_module, "load_retrieval_config", counting_loader)
    monkeypatch.setattr(deps_module, "load_retrieval_config", counting_loader)
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        assert calls == [1], "lifespan should parse the config exactly once"
        request = SimpleNamespace(app=client.app)
        for _ in range(5):
            get_retrieval_config(request)  # type: ignore[arg-type]
        assert calls == [1], "the dependency must read app.state, not re-parse"


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


def test_lifespan_propagates_database_init_error(
    models_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#54: a failing Database.__init__ must propagate, not be swallowed.

    Lifespan errors surface as an exception during ``TestClient(create_app()).__enter__()``.
    The app refuses to come up — operators see a clear traceback instead
    of a silently-broken instance.
    """
    monkeypatch.setenv(
        "KILN_DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/d"
    )  # pragma: allowlist secret

    def boom(*a: object, **kw: object) -> object:
        raise RuntimeError("simulated init failure")

    # cf_knowledge_kiln.api package's __init__ does
    # ``from .app import app`` which shadows the module name with the
    # FastAPI instance, so the usual dotted-path monkeypatch resolves
    # to the wrong object. Reach into sys.modules to grab the actual
    # module, then patch its Database binding.
    import sys

    app_module = sys.modules["cf_knowledge_kiln.api.app"]
    monkeypatch.setattr(app_module, "Database", boom)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="simulated init failure"), TestClient(create_app()):
        pass


def test_probe_embedding_honors_configured_timeout(
    models_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#198: KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS is wired all the way through.

    Pre-fix the timeout was a hardcoded module-level 30s constant, so a
    cold HuggingFace weight pull on first start tripped the probe and
    pinned ``app.state.embedding_status = "failing"`` for the life of
    the process. The fix makes the timeout settings-driven; this test
    proves the setting actually reaches ``_probe_embedding`` by giving
    it an absurdly small timeout against a sleep-then-return provider
    and asserting the probe reports ``failing``.
    """
    import asyncio
    import sys

    monkeypatch.setenv("KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()

    class _SlowProvider:
        provider = "slow"
        model = "slow-model"
        dimensions = 768

        async def embed(self, texts: list[str]) -> list[list[float]]:
            # Sleep well past the 0.05s timeout so asyncio.wait_for trips.
            await asyncio.sleep(0.5)
            return [[0.0] * 768 for _ in texts]

        async def aclose(self) -> None:
            return None

    app_module = sys.modules["cf_knowledge_kiln.api.app"]

    def _factory(_settings: object) -> _SlowProvider:
        return _SlowProvider()

    monkeypatch.setattr(app_module, "build_provider_from_settings", _factory)

    with TestClient(create_app()) as client:
        assert client.app.state.embedding_status == "failing"


def test_lifespan_disposes_database_even_on_shutdown_path(
    models_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#54: a failing Database.dispose() bubbles up from teardown.

    Verifies the dispose call actually fires (so a leaking pool would
    surface in tests as a noisy teardown rather than silently linger).
    """

    monkeypatch.setenv(
        "KILN_DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/d"
    )  # pragma: allowlist secret
    from cf_knowledge_kiln.db.connection import Database

    dispose_calls: list[int] = []

    async def counting_dispose(self: object) -> None:
        dispose_calls.append(1)

    monkeypatch.setattr(Database, "dispose", counting_dispose)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
    # __exit__ ran the lifespan teardown which must have called dispose.
    assert dispose_calls == [1]
