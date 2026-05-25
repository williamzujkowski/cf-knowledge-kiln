"""Unit tests for the embedding-provider config loader + factory.

The factory enforces three policies that the rest of Phase 4 trusts:

1. Provider selection is config (``config/models.yaml``), not code.
2. Excluded model families (Qwen / DeepSeek / BAAI/BGE — per
   ADR-0005 + docs/model-providers.md) are refused at construction
   time, not at use time. The loader is the gate.
3. Required env vars for HTTP adapters are validated up front so the
   worker doesn't crash mid-batch.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.factory import (
    _PROVIDER_FACTORIES,
    EmbeddingConfig,
    EmbeddingConfigError,
    build_embedding_provider,
    build_provider_from_settings,
    load_embedding_config,
)
from cf_knowledge_kiln.ingestion.embedding.local import (
    LocalEmbeddingProvider,
    LocalSentenceTransformersProvider,
)
from cf_knowledge_kiln.ingestion.embedding.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _settings(**overrides: object) -> Settings:
    # Force-disable env-file loading so a real .env can't leak into tests.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type, call-arg]


class TestLoadEmbeddingConfig:
    def test_minimal_valid_config(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: true
""",
        )
        config = load_embedding_config(str(path))
        assert config.provider == "mock"
        assert config.name == "mock-768"
        assert config.dimensions == 768
        assert config.enabled is True

    def test_trust_remote_code_defaults_false(self, tmp_path: Path) -> None:
        """Omitting the key means remote code is NOT trusted (secure default)."""
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: true
""",
        )
        config = load_embedding_config(str(path))
        assert config.trust_remote_code is False

    def test_trust_remote_code_parsed_from_yaml(self, tmp_path: Path) -> None:
        """Operators opt into remote code per model via config, not code."""
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: local
    name: nomic-ai/nomic-embed-text-v1.5
    dimensions: 768
    enabled: true
    trust_remote_code: true
""",
        )
        config = load_embedding_config(str(path))
        assert config.trust_remote_code is True

    def test_openai_compatible_with_env_pointers(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: openai-compatible
    name: text-embedding-3-small
    dimensions: 1536
    enabled: true
    base_url_env: KILN_EMBEDDING_BASE_URL
    api_key_env: KILN_EMBEDDING_API_KEY
providers:
  openai_compatible:
    timeout_seconds: 30
    max_retries: 4
""",
        )
        config = load_embedding_config(str(path))
        assert config.provider == "openai-compatible"
        assert config.base_url_env == "KILN_EMBEDDING_BASE_URL"
        assert config.api_key_env == "KILN_EMBEDDING_API_KEY"  # pragma: allowlist secret
        assert config.provider_settings.timeout_seconds == 30
        assert config.provider_settings.max_retries == 4

    def test_unknown_provider_rejected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: voodoo-ml
    name: x
    dimensions: 32
    enabled: true
""",
        )
        with pytest.raises(EmbeddingConfigError, match="provider"):
            load_embedding_config(str(path))

    def test_excluded_model_family_rejected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: openai-compatible
    name: BAAI/bge-large-en-v1.5
    dimensions: 1024
    enabled: true
""",
        )
        with pytest.raises(EmbeddingConfigError, match="excluded"):
            load_embedding_config(str(path))

    def test_qwen_excluded(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: local
    name: Qwen/Qwen3-Embedding-8B
    dimensions: 1024
    enabled: true
""",
        )
        with pytest.raises(EmbeddingConfigError, match="excluded"):
            load_embedding_config(str(path))

    def test_local_sentence_transformers_provider_name_accepted(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: local-sentence-transformers
    name: nomic-ai/nomic-embed-text-v1.5
    dimensions: 768
    enabled: true
""",
        )
        config = load_embedding_config(str(path))
        assert config.provider == "local-sentence-transformers"
        assert config.name == "nomic-ai/nomic-embed-text-v1.5"
        assert config.dimensions == 768

    def test_excluded_check_applies_to_local_sentence_transformers_provider(
        self, tmp_path: Path
    ) -> None:
        """Exclusion list applies regardless of which local-* alias is used."""
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: local-sentence-transformers
    name: BAAI/bge-base-en-v1.5
    dimensions: 768
    enabled: true
""",
        )
        with pytest.raises(EmbeddingConfigError, match="excluded"):
            load_embedding_config(str(path))

    def test_deepseek_excluded(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: local
    name: deepseek-ai/something-embedding
    dimensions: 1024
    enabled: true
""",
        )
        with pytest.raises(EmbeddingConfigError, match="excluded"):
            load_embedding_config(str(path))

    def test_disabled_model_is_loadable_but_factory_refuses(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: false
""",
        )
        config = load_embedding_config(str(path))
        assert config.enabled is False
        with pytest.raises(EmbeddingConfigError, match="disabled"):
            build_embedding_provider(config, _settings())

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(EmbeddingConfigError, match="not found"):
            load_embedding_config(str(tmp_path / "nope.yaml"))

    def test_shipped_example_config_loads(self) -> None:
        """The shipped ``config/models.example.yaml`` must parse cleanly.

        The example file is what operators copy AND what the kiln
        falls back to when ``config/models.yaml`` doesn't exist
        (#241). Two regressions this guards:

        * **It must load** — a bare name, a typo, or a missing
          required field would ship a config that can't construct a
          provider.
        * **It must use the mock provider out of the box** (#241)
          so a fresh CF deploy boots cleanly without weights or
          network. Operators uncomment one of the real-provider
          blocks for production.
        """
        example = Path(__file__).resolve().parents[2] / "config" / "models.example.yaml"
        assert example.exists(), f"missing fixture: {example}"
        config = load_embedding_config(example)
        assert config.provider == "mock"
        assert config.dimensions == 384
        # Mock provider doesn't need trust_remote_code; default False.
        assert config.trust_remote_code is False

    def test_dimensions_must_be_positive(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-bad
    dimensions: 0
    enabled: true
""",
        )
        with pytest.raises(EmbeddingConfigError, match="dimensions"):
            load_embedding_config(str(path))


class TestBuildEmbeddingProvider:
    def test_mock_factory(self) -> None:
        config = EmbeddingConfig(provider="mock", name="mock-768", dimensions=768, enabled=True)
        provider = build_embedding_provider(config, _settings())
        assert isinstance(provider, MockEmbeddingProvider)
        assert provider.dimensions == 768

    def test_local_factory(self) -> None:
        config = EmbeddingConfig(
            provider="local",
            name="nomic-embed-text-v1.5",
            dimensions=768,
            enabled=True,
        )
        # Inject a no-op factory so the test doesn't load real weights.
        # The factory receives device + trust_remote_code kwargs; the
        # double takes **_ so the contract can grow without churn.
        provider = build_embedding_provider(
            config,
            _settings(),
            local_model_factory=lambda _name, **_: object(),
        )
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider.model == "nomic-embed-text-v1.5"

    def test_local_factory_passes_trust_remote_code(self) -> None:
        """#NNN — the config's trust_remote_code reaches the provider."""
        config = EmbeddingConfig(
            provider="local",
            name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            enabled=True,
            trust_remote_code=True,
        )
        provider = build_embedding_provider(
            config,
            _settings(),
            local_model_factory=lambda _name, **_: object(),
        )
        assert isinstance(provider, LocalSentenceTransformersProvider)
        assert provider.trust_remote_code is True

    def test_local_factory_trust_remote_code_defaults_false(self) -> None:
        config = EmbeddingConfig(
            provider="local",
            name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            enabled=True,
        )
        provider = build_embedding_provider(
            config,
            _settings(),
            local_model_factory=lambda _name, **_: object(),
        )
        assert isinstance(provider, LocalSentenceTransformersProvider)
        assert provider.trust_remote_code is False

    def test_local_sentence_transformers_alias(self) -> None:
        """``local-sentence-transformers`` is the canonical provider name."""
        config = EmbeddingConfig(
            provider="local-sentence-transformers",
            name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            enabled=True,
        )
        provider = build_embedding_provider(
            config,
            _settings(),
            local_model_factory=lambda _name, **_: object(),
        )
        assert isinstance(provider, LocalSentenceTransformersProvider)
        assert provider.model == "nomic-ai/nomic-embed-text-v1.5"

    def test_registry_pattern_exposes_factories(self) -> None:
        """Adding a new backend should be one entry in the registry dict."""
        # The registry is the single dispatch table; if this changes
        # shape, callers (and reviewers) need to know.
        assert "mock" in _PROVIDER_FACTORIES
        assert "local" in _PROVIDER_FACTORIES
        assert "local-sentence-transformers" in _PROVIDER_FACTORIES
        assert "openai-compatible" in _PROVIDER_FACTORIES

    def test_openai_compatible_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_EMBEDDING_API_KEY", "sk-test")
        monkeypatch.setenv("KILN_EMBEDDING_BASE_URL", "https://api.example.test")
        config = EmbeddingConfig(
            provider="openai-compatible",
            name="text-embedding-3-small",
            dimensions=1536,
            enabled=True,
            base_url_env="KILN_EMBEDDING_BASE_URL",
            api_key_env="KILN_EMBEDDING_API_KEY",  # pragma: allowlist secret
        )
        provider = build_embedding_provider(config, _settings())
        try:
            assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
            assert provider.model == "text-embedding-3-small"
            assert provider.dimensions == 1536
        finally:
            # Synchronous test, async aclose is awkward — just close
            # the underlying client.
            import asyncio

            asyncio.run(provider.aclose())

    def test_openai_compatible_refuses_without_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KILN_EMBEDDING_API_KEY", raising=False)
        monkeypatch.setenv("KILN_EMBEDDING_BASE_URL", "https://api.example.test")
        config = EmbeddingConfig(
            provider="openai-compatible",
            name="text-embedding-3-small",
            dimensions=1536,
            enabled=True,
            base_url_env="KILN_EMBEDDING_BASE_URL",
            api_key_env="KILN_EMBEDDING_API_KEY",  # pragma: allowlist secret
        )
        with pytest.raises(EmbeddingConfigError, match="KILN_EMBEDDING_API_KEY"):
            build_embedding_provider(config, _settings())

    def test_openai_compatible_refuses_without_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KILN_EMBEDDING_API_KEY", "sk-test")
        monkeypatch.delenv("KILN_EMBEDDING_BASE_URL", raising=False)
        config = EmbeddingConfig(
            provider="openai-compatible",
            name="text-embedding-3-small",
            dimensions=1536,
            enabled=True,
            base_url_env="KILN_EMBEDDING_BASE_URL",
            api_key_env="KILN_EMBEDDING_API_KEY",  # pragma: allowlist secret
        )
        with pytest.raises(EmbeddingConfigError, match="KILN_EMBEDDING_BASE_URL"):
            build_embedding_provider(config, _settings())


class TestBuildProviderFromSettings:
    """The shared startup helper used by the worker AND the API lifespan (#58)."""

    def test_missing_config_returns_none_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _settings(models_config_path=str(tmp_path / "absent.yaml"))
        with caplog.at_level(logging.WARNING):
            result = build_provider_from_settings(settings)
        assert result is None
        assert any("no embedding config" in r.getMessage() for r in caplog.records)

    def test_excluded_model_raises_at_startup(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: local
    name: Qwen/Qwen3-Embedding-8B
    dimensions: 1024
    enabled: true
""",
        )
        settings = _settings(models_config_path=str(path))
        with pytest.raises(EmbeddingConfigError, match="excluded"):
            build_provider_from_settings(settings)

    def test_valid_mock_config_returns_provider(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: true
""",
        )
        settings = _settings(models_config_path=str(path))
        provider = build_provider_from_settings(settings)
        assert isinstance(provider, MockEmbeddingProvider)
        assert provider.dimensions == 768

    def test_disabled_model_is_fatal_at_startup(self, tmp_path: Path) -> None:
        """An operator who wrote enabled=false in production wants to know."""
        path = _write(
            tmp_path / "models.yaml",
            """
models:
  embedding:
    provider: mock
    name: mock-768
    dimensions: 768
    enabled: false
""",
        )
        settings = _settings(models_config_path=str(path))
        with pytest.raises(EmbeddingConfigError, match="disabled"):
            build_provider_from_settings(settings)
