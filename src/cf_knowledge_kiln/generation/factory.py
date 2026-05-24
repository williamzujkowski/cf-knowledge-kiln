"""Active-generator config loader + provider factory (#192 Phase A).

Mirrors :mod:`cf_knowledge_kiln.ingestion.embedding.factory`. Reads
``config/models.yaml::models.generator``, validates against the
provenance allowlist documented in :file:`docs/model-providers.md`,
and returns the active :class:`GeneratorProvider`.

Same policies as the embedding factory:

* **Provider selection is config, not code.** Backends dispatch through
  the :data:`_PROVIDER_FACTORIES` registry — adding a new backend is
  one entry in that dict.
* **Excluded model families refused at config-load time** (Qwen,
  DeepSeek, BGE per ADR-0005). Naming any of them raises before the
  endpoint can be invoked.
* **Required env vars validated up front.** An ``openai-compatible``
  generator with no ``KILN_GENERATOR_API_KEY`` or
  ``KILN_GENERATOR_BASE_URL`` refuses to start.

Note: ``models.generator.enabled: false`` (the MVP default) is **not**
an error — it returns ``None``. ``/v1/answer`` callers must detect
``None`` and refuse with a clear "no generator configured" 503 rather
than throwing. This mirrors the embedding factory's "missing config
file = None" semantics but keyed on the explicit ``enabled`` flag.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.generation import (
    GeneratorProvider,
    MockGeneratorProvider,
)
from cf_knowledge_kiln.generation.openai_compatible import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    OpenAICompatibleGeneratorProvider,
)

logger = logging.getLogger(__name__)

ProviderName = Literal["openai-compatible", "mock"]

# Substring match, case-insensitive. Mirrors docs/model-providers.md
# and the embedding factory's allowlist — same enforcement, same set.
_EXCLUDED_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen",
    "deepseek",
    "baai/bge",
    "bge-",
)


class GeneratorConfigError(ValueError):
    """Raised when ``config/models.yaml::models.generator`` is malformed or excluded."""


class _ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    concurrency: int = DEFAULT_CONCURRENCY


class GeneratorConfig(BaseModel):
    """Active generator model + provider settings loaded from YAML."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str
    name: str
    enabled: bool = True
    base_url_env: str | None = None
    api_key_env: str | None = None
    default_temperature: float = 0.0
    default_max_tokens: int = 1024
    provider_settings: _ProviderSettings = Field(default_factory=_ProviderSettings)


# ───────────────────────── Provider registry ────────────────────────

ProviderFactory = Callable[[GeneratorConfig, Settings], GeneratorProvider]


def _mock_factory(config: GeneratorConfig, _settings: Settings) -> GeneratorProvider:
    return MockGeneratorProvider(model=config.name)


def _openai_compatible_factory(config: GeneratorConfig, settings: Settings) -> GeneratorProvider:
    base_url = _resolve_env(config.base_url_env, settings.generator_base_url)
    api_key = _resolve_env(config.api_key_env, settings.generator_api_key)
    if not base_url:
        env_name = config.base_url_env or "KILN_GENERATOR_BASE_URL"
        raise GeneratorConfigError(f"openai-compatible generator requires {env_name} to be set")
    if not api_key:
        env_name = config.api_key_env or "KILN_GENERATOR_API_KEY"
        raise GeneratorConfigError(f"openai-compatible generator requires {env_name} to be set")
    return OpenAICompatibleGeneratorProvider.from_url(
        base_url=base_url,
        model=config.name,
        api_key=api_key,
        timeout_seconds=config.provider_settings.timeout_seconds,
        concurrency=config.provider_settings.concurrency,
        max_retries=config.provider_settings.max_retries,
    )


_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "mock": _mock_factory,
    "openai-compatible": _openai_compatible_factory,
}


def load_generator_config(path: str | Path) -> GeneratorConfig | None:
    """Read ``config/models.yaml`` and validate the generator section.

    Returns ``None`` when:

    * the file is missing (worker / API still runs without generation),
    * the file has no ``models.generator`` block,
    * the generator block has ``enabled: false`` (the MVP default).

    Raises :class:`GeneratorConfigError` for malformed YAML, unknown
    providers, excluded model families, and schema violations.
    """
    p = Path(path)
    if not p.exists():
        logger.info("no generator config at %s; /v1/answer will report no-generator", p)
        return None
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GeneratorConfigError(f"malformed YAML in {p}: {exc}") from exc

    models = raw.get("models") or {}
    generator = models.get("generator")
    if not isinstance(generator, dict):
        logger.info("%s has no models.generator block; /v1/answer will report no-generator", p)
        return None

    providers = raw.get("providers") or {}
    openai_settings = providers.get("openai_compatible") or {}
    payload = dict(generator)
    # Merge provider-level settings if present, else use defaults.
    payload.setdefault("provider_settings", openai_settings)

    try:
        config = GeneratorConfig.model_validate(payload)
    except ValidationError as exc:
        raise GeneratorConfigError(f"{p}: {exc}") from exc

    if not config.enabled:
        logger.info(
            "generator model %r is disabled in %s; /v1/answer will report no-generator",
            config.name,
            p,
        )
        return None

    _validate_policies(config)
    return config


def _validate_policies(config: GeneratorConfig) -> None:
    if config.provider not in _PROVIDER_FACTORIES:
        raise GeneratorConfigError(
            f"unknown generator provider {config.provider!r}; "
            f"allowed: {sorted(_PROVIDER_FACTORIES)}"
        )
    lower = config.name.lower()
    for marker in _EXCLUDED_MODEL_PREFIXES:
        if marker in lower:
            raise GeneratorConfigError(
                f"model {config.name!r} is on the excluded list "
                f"(matches {marker!r}); see docs/model-providers.md"
            )


def build_generator_provider(config: GeneratorConfig, settings: Settings) -> GeneratorProvider:
    """Instantiate the active provider per ``config``.

    Callers that should tolerate "no generator" (e.g. the API lifespan)
    use :func:`build_generator_from_settings` instead — it returns
    ``None`` when no generator is configured. This function is the
    "I already know I have a config" path and raises on misconfig.
    """
    try:
        factory = _PROVIDER_FACTORIES[config.provider]
    except KeyError as exc:
        # ``_validate_policies`` should have caught this, but defend
        # in depth if a caller built a GeneratorConfig directly.
        raise GeneratorConfigError(f"unhandled provider {config.provider!r}") from exc
    return factory(config, settings)


def _resolve_env(env_name: str | None, fallback: str | None) -> str | None:
    """Read ``env_name`` from the OS environment; fall back to settings."""
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value
    return fallback


def build_generator_from_settings(settings: Settings) -> GeneratorProvider | None:
    """Build the active generator provider, tolerating missing/disabled config.

    Both the API lifespan and tests call this at startup. Policy:

    * **Missing ``config/models.yaml`` or absent generator block** —
      info-log, return ``None``. ``/v1/answer`` callers detect this and
      respond with a clear "no generator configured" path.
    * **``enabled: false``** — same as missing (the MVP default).
      Documented; this is how /v1/answer stays disabled by default.
    * **Malformed or excluded-model config** — fatal. Raise
      :class:`GeneratorConfigError` so startup fails loudly rather
      than silently skipping the generator.

    Path comes from :attr:`Settings.models_config_path` (default
    ``config/models.yaml``); operators override via
    ``KILN_MODELS_CONFIG_PATH``.
    """
    path = Path(settings.models_config_path)
    try:
        config = load_generator_config(path)
    except GeneratorConfigError:
        logger.exception("invalid generator config at %s", path)
        raise
    if config is None:
        return None
    return build_generator_provider(config, settings)


__all__: list[str] = [
    "GeneratorConfig",
    "GeneratorConfigError",
    "ProviderName",
    "build_generator_from_settings",
    "build_generator_provider",
    "load_generator_config",
]
