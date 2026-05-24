"""Unit tests for the generator factory (#192 Phase A).

Mirrors :mod:`tests.unit.test_ingestion_embedding_factory`. Covers:

* missing config → None (gracefully degraded — /v1/answer reports
  no-generator instead of 503ing).
* disabled config → None (the MVP default).
* malformed YAML → GeneratorConfigError.
* unknown provider → GeneratorConfigError.
* excluded-model-family check → GeneratorConfigError.
* env-var resolution for openai-compatible.
* mock provider construction.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.generation import (
    MockGeneratorProvider,
    OpenAICompatibleGeneratorProvider,
)
from cf_knowledge_kiln.generation.factory import (
    GeneratorConfigError,
    build_generator_from_settings,
    build_generator_provider,
    load_generator_config,
)


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "models.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "models_config_path": str(tmp_path / "models.yaml"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestLoadGeneratorConfig:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_generator_config(tmp_path / "absent.yaml") is None

    def test_missing_generator_block_returns_none(self, tmp_path: Path) -> None:
        """A file with only the embedding block must NOT fail — /v1/answer
        is opt-in and absence is not an error.
        """
        path = _write_yaml(
            tmp_path,
            """
            models:
              embedding:
                provider: mock
                name: mock-768
                dimensions: 768
                enabled: true
            """,
        )
        assert load_generator_config(path) is None

    def test_disabled_generator_returns_none(self, tmp_path: Path) -> None:
        """The MVP default — disabled means no /v1/answer, no exception."""
        path = _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: openai-compatible
                name: phi-4-mini-instruct
                enabled: false
            """,
        )
        assert load_generator_config(path) is None

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "models.yaml"
        path.write_text("models: : :\n", encoding="utf-8")
        with pytest.raises(GeneratorConfigError, match="malformed YAML"):
            load_generator_config(path)

    def test_unknown_provider_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: nope
                name: anything
                enabled: true
            """,
        )
        with pytest.raises(GeneratorConfigError, match="unknown generator provider"):
            load_generator_config(path)

    @pytest.mark.parametrize(
        "name",
        ["Qwen/Qwen2.5-7B", "deepseek-ai/DeepSeek-V2", "BAAI/bge-m3"],
    )
    def test_excluded_model_families_refused(self, tmp_path: Path, name: str) -> None:
        path = _write_yaml(
            tmp_path,
            f"""
            models:
              generator:
                provider: openai-compatible
                name: {name}
                enabled: true
            """,
        )
        with pytest.raises(GeneratorConfigError, match="excluded list"):
            load_generator_config(path)

    def test_valid_openai_compatible_config_returns_config(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: openai-compatible
                name: phi-4-mini-instruct
                enabled: true
                base_url_env: KILN_GENERATOR_BASE_URL
                api_key_env: KILN_GENERATOR_API_KEY
                default_temperature: 0.0
                default_max_tokens: 512
            """,
        )
        config = load_generator_config(path)
        assert config is not None
        assert config.provider == "openai-compatible"
        assert config.name == "phi-4-mini-instruct"
        assert config.enabled is True
        assert config.default_max_tokens == 512


class TestBuildGeneratorProvider:
    async def test_mock_provider_round_trip(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: mock
                name: mock-generator
                enabled: true
            """,
        )
        config = load_generator_config(path)
        assert config is not None
        provider = build_generator_provider(config, _settings(tmp_path))
        assert isinstance(provider, MockGeneratorProvider)
        result = await provider.generate("test", max_tokens=8)
        assert "test" in result.text

    def test_openai_compatible_built_from_settings_and_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: openai-compatible
                name: phi-4-mini-instruct
                enabled: true
                base_url_env: KILN_GENERATOR_BASE_URL
                api_key_env: KILN_GENERATOR_API_KEY
            """,
        )
        monkeypatch.setenv("KILN_GENERATOR_BASE_URL", "https://gen.example.test")
        monkeypatch.setenv("KILN_GENERATOR_API_KEY", "sk-test")
        config = load_generator_config(path)
        assert config is not None
        provider = build_generator_provider(config, _settings(tmp_path))
        assert isinstance(provider, OpenAICompatibleGeneratorProvider)
        assert provider.model == "phi-4-mini-instruct"

    def test_openai_compatible_missing_base_url_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: openai-compatible
                name: phi-4-mini-instruct
                enabled: true
                base_url_env: KILN_GENERATOR_BASE_URL
                api_key_env: KILN_GENERATOR_API_KEY
            """,
        )
        monkeypatch.delenv("KILN_GENERATOR_BASE_URL", raising=False)
        monkeypatch.setenv("KILN_GENERATOR_API_KEY", "sk-test")
        config = load_generator_config(path)
        assert config is not None
        with pytest.raises(GeneratorConfigError, match="KILN_GENERATOR_BASE_URL"):
            build_generator_provider(config, _settings(tmp_path))

    def test_openai_compatible_missing_api_key_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: openai-compatible
                name: phi-4-mini-instruct
                enabled: true
                base_url_env: KILN_GENERATOR_BASE_URL
                api_key_env: KILN_GENERATOR_API_KEY
            """,
        )
        monkeypatch.setenv("KILN_GENERATOR_BASE_URL", "https://gen.example.test")
        monkeypatch.delenv("KILN_GENERATOR_API_KEY", raising=False)
        config = load_generator_config(path)
        assert config is not None
        with pytest.raises(GeneratorConfigError, match="KILN_GENERATOR_API_KEY"):
            build_generator_provider(config, _settings(tmp_path))


class TestBuildFromSettings:
    def test_missing_models_yaml_returns_none(self, tmp_path: Path) -> None:
        """Same graceful-degradation as load_generator_config — the API
        starts without a generator and /v1/answer reports no-generator.
        """
        result = build_generator_from_settings(_settings(tmp_path))
        assert result is None

    def test_disabled_generator_returns_none(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: openai-compatible
                name: phi-4-mini-instruct
                enabled: false
            """,
        )
        assert build_generator_from_settings(_settings(tmp_path)) is None

    async def test_mock_provider_end_to_end(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path,
            """
            models:
              generator:
                provider: mock
                name: mock-generator
                enabled: true
            """,
        )
        provider = build_generator_from_settings(_settings(tmp_path))
        assert provider is not None
        assert isinstance(provider, MockGeneratorProvider)
        result = await provider.generate("hello", max_tokens=16)
        assert "hello" in result.text
