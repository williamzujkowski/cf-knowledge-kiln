"""Active-model config loader + provider factory.

Reads ``config/models.yaml`` (the same schema as
``config/models.example.yaml``), validates it against the provenance
allowlist documented in :file:`docs/model-providers.md`, and returns
the active :class:`EmbeddingProvider`.

Three policies are enforced here so the rest of the codebase can trust
the provider it receives:

* **Provider selection is config, not code.** A model swap is a YAML
  change.
* **Excluded model families are refused at config-load time.** Qwen,
  DeepSeek, and BAAI/BGE are the plan's hard exclusion list (ADR-0005).
  Naming any of them in ``config/models.yaml`` raises before the worker
  ever starts. This is a defense in depth on top of the human-review
  gate on ``docs/model-providers.md``.
* **Required env vars are validated up front.** An HTTP adapter that
  needs ``KILN_EMBEDDING_API_KEY`` refuses to start without it instead
  of failing mid-batch.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.ingestion.embedding import (
    DEFAULT_DIMENSIONS,
    EmbeddingProvider,
    MockEmbeddingProvider,
)
from cf_knowledge_kiln.ingestion.embedding.local import (
    LocalEmbeddingProvider,
    ModelFactory,
)
from cf_knowledge_kiln.ingestion.embedding.openai_compatible import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    OpenAICompatibleEmbeddingProvider,
)

logger = logging.getLogger(__name__)

ProviderName = Literal["local", "openai-compatible", "mock"]

_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"local", "openai-compatible", "mock"})

# Substring match, case-insensitive. Mirrors docs/model-providers.md.
_EXCLUDED_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen",
    "deepseek",
    "baai/bge",
    "bge-",
)


class EmbeddingConfigError(ValueError):
    """Raised when ``config/models.yaml`` violates a policy or is malformed."""


class _ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES


class EmbeddingConfig(BaseModel):
    """Active embedding model + provider settings loaded from YAML."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str
    name: str
    dimensions: int = DEFAULT_DIMENSIONS
    enabled: bool = True
    base_url_env: str | None = None
    api_key_env: str | None = None
    provider_settings: _ProviderSettings = Field(default_factory=_ProviderSettings)


def load_embedding_config(path: str | Path) -> EmbeddingConfig:
    """Read ``config/models.yaml`` and validate the embedding section.

    Raises :class:`EmbeddingConfigError` for missing files, unknown
    providers, excluded model families, and malformed schemas.
    """
    p = Path(path)
    if not p.exists():
        raise EmbeddingConfigError(f"embedding config not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EmbeddingConfigError(f"malformed YAML in {p}: {exc}") from exc

    models = raw.get("models") or {}
    embedding = models.get("embedding")
    if not isinstance(embedding, dict):
        raise EmbeddingConfigError(f"{p} is missing models.embedding")

    providers = raw.get("providers") or {}
    openai_settings = providers.get("openai_compatible") or {}
    payload = dict(embedding)
    payload["provider_settings"] = openai_settings

    try:
        config = EmbeddingConfig.model_validate(payload)
    except ValidationError as exc:
        raise EmbeddingConfigError(f"{p}: {exc}") from exc

    _validate_policies(config)
    return config


def _validate_policies(config: EmbeddingConfig) -> None:
    if config.provider not in _ALLOWED_PROVIDERS:
        raise EmbeddingConfigError(
            f"unknown embedding provider {config.provider!r}; allowed: {sorted(_ALLOWED_PROVIDERS)}"
        )
    if config.dimensions <= 0:
        raise EmbeddingConfigError(f"dimensions must be positive, got {config.dimensions}")
    lower = config.name.lower()
    for marker in _EXCLUDED_MODEL_PREFIXES:
        if marker in lower:
            raise EmbeddingConfigError(
                f"model {config.name!r} is on the excluded list "
                f"(matches {marker!r}); see docs/model-providers.md"
            )


def build_embedding_provider(
    config: EmbeddingConfig,
    settings: Settings,
    *,
    local_model_factory: ModelFactory | None = None,
) -> EmbeddingProvider:
    """Instantiate the active provider per ``config``.

    The factory honors the policy that a disabled model never produces
    a working provider — even if everything else is valid.

    ``local_model_factory`` is injectable so tests can avoid loading
    real sentence-transformers weights.
    """
    if not config.enabled:
        raise EmbeddingConfigError(f"embedding model {config.name!r} is disabled in config")
    if config.provider == "mock":
        return MockEmbeddingProvider(dimensions=config.dimensions, model=config.name)
    if config.provider == "local":
        return LocalEmbeddingProvider(
            model=config.name,
            dimensions=config.dimensions,
            model_factory=local_model_factory,
        )
    if config.provider == "openai-compatible":
        return _build_openai_compatible(config, settings)
    raise EmbeddingConfigError(f"unhandled provider {config.provider!r}")


def _build_openai_compatible(
    config: EmbeddingConfig, settings: Settings
) -> OpenAICompatibleEmbeddingProvider:
    base_url = _resolve_env(config.base_url_env, settings.embedding_base_url)
    api_key = _resolve_env(config.api_key_env, settings.embedding_api_key)
    if not base_url:
        env_name = config.base_url_env or "KILN_EMBEDDING_BASE_URL"
        raise EmbeddingConfigError(f"openai-compatible provider requires {env_name} to be set")
    if not api_key:
        env_name = config.api_key_env or "KILN_EMBEDDING_API_KEY"
        raise EmbeddingConfigError(f"openai-compatible provider requires {env_name} to be set")
    return OpenAICompatibleEmbeddingProvider.from_url(
        base_url=base_url,
        model=config.name,
        dimensions=config.dimensions,
        api_key=api_key,
        timeout_seconds=config.provider_settings.timeout_seconds,
        concurrency=settings.ingest_concurrency,
        max_retries=config.provider_settings.max_retries,
    )


def _resolve_env(env_name: str | None, fallback: str | None) -> str | None:
    """Read ``env_name`` from the OS environment; fall back to settings."""
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value
    return fallback


def build_provider_from_settings(settings: Settings) -> EmbeddingProvider | None:
    """Build the active embedding provider, tolerating a missing config file.

    Both the worker and the API call this at startup. Policy:

    * **Missing ``config/models.yaml``** — log a warning, return ``None``.
      The worker continues without an embedding pass; the API may still
      serve health endpoints. Callers must check for ``None`` before
      using the result.
    * **Malformed or excluded-model config** — fatal. Raise
      :class:`EmbeddingConfigError` so startup fails loudly rather than
      silently skipping embeddings.

    Path comes from :attr:`Settings.models_config_path` (default
    ``config/models.yaml``); operators override via
    ``KILN_MODELS_CONFIG_PATH``.
    """
    path = Path(settings.models_config_path)
    if not path.exists():
        logger.warning(
            "no embedding config at %s; this process will not generate embeddings",
            path,
        )
        return None
    try:
        config = load_embedding_config(path)
        return build_embedding_provider(config, settings)
    except EmbeddingConfigError:
        logger.exception("invalid embedding config at %s", path)
        raise


__all__: list[Any] = [
    "EmbeddingConfig",
    "EmbeddingConfigError",
    "ProviderName",
    "build_embedding_provider",
    "build_provider_from_settings",
    "load_embedding_config",
]
