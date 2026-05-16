"""Application settings.

Precedence: environment variables > .env file > defaults. Secret-bearing
fields are always env-only; they are never read from YAML config so that
secrets cannot land in the source tree.

In Cloud Foundry, the database URL is normally not set directly; the app
reads ``VCAP_SERVICES`` and resolves the bound Postgres credentials at
startup. See ``cf_knowledge_kiln.db`` (Phase 2+).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
AuthMode = Literal["none", "bearer", "mtls"]


class Settings(BaseSettings):
    """Runtime settings, loaded from env + .env."""

    model_config = SettingsConfigDict(
        env_prefix="KILN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "cf-knowledge-kiln"
    env: Environment = "development"
    log_level: LogLevel = "INFO"
    http_port: int = 8080

    # Database — optional locally; required in CF (via service binding).
    database_url: str | None = None
    pg_service_name: str = "cf-knowledge-kiln-db"
    pg_pool_size: int = 5
    pg_pool_max_overflow: int = 10

    # Embedding provider — see config/models.yaml for the swappable registry.
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None

    # Optional generator (for /v1/answer).
    generator_api_key: str | None = None
    generator_base_url: str | None = None

    # Ingestion.
    ingest_concurrency: int = 4
    ingest_max_file_bytes: int = 1_048_576

    # Retrieval defaults.
    default_max_chunks: int = 8
    default_max_tokens: int = 3000
    default_status_preference: str = "active,approved"

    # Security.
    source_allowlist_path: str = "config/sources.yaml"
    auth_mode: AuthMode = "none"
    bearer_token: str | None = None

    # Telemetry.
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "cf-knowledge-kiln"

    @property
    def status_preference_list(self) -> list[str]:
        """Parse the comma-separated preference string into a list."""
        return [s.strip() for s in self.default_status_preference.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance.

    Tests that need a fresh settings instance should call
    ``get_settings.cache_clear()`` first.
    """
    return Settings()
