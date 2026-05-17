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

from pathlib import Path

import pytest

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.factory import (
    EmbeddingConfig,
    EmbeddingConfigError,
    build_embedding_provider,
    load_embedding_config,
)
from cf_knowledge_kiln.ingestion.embedding.local import LocalEmbeddingProvider
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
        provider = build_embedding_provider(
            config, _settings(), local_model_factory=lambda _name: object()
        )
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider.model == "nomic-embed-text-v1.5"

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
