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
    # Per-process pool: up to pg_pool_size + pg_pool_max_overflow
    # connections. Budget against Postgres max_connections summed over
    # every process (KILN_WEB_WORKERS api workers + 1 worker app) —
    # see "Connection pool sizing" in docs/configuration.md before
    # raising the worker count.
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
    ingest_max_files: int = 10_000
    ingest_max_repo_bytes: int = 100 * 1_048_576
    ingest_poll_interval_seconds: float = 5.0
    # Embedding fan-out tuning (PR C, prep for #108 item 2). The
    # ingestion pipeline batches chunks into groups of
    # ``ingest_embed_batch_size`` and runs up to
    # ``ingest_embed_concurrency`` batches in parallel against the
    # configured embedding provider. Defaults sized for a
    # CPU-backed local provider: 32 keeps the per-call latency
    # bounded, 4 keeps total worker threads reasonable when
    # OMP_NUM_THREADS=2.
    ingest_embed_batch_size: int = 32
    ingest_embed_concurrency: int = 4

    # Retrieval defaults.
    default_max_chunks: int = 8
    default_max_tokens: int = 3000
    default_status_preference: str = "active,approved"
    # Per ADR-0009 §1 — hnsw.ef_search controls recall/latency on the
    # vector arm. The pgvector default (40) is too low for the
    # recall@10 target on small corpora.
    hnsw_ef_search: int = 200

    # Embedding provider config (Phase 4).
    models_config_path: str = "config/models.yaml"

    # Security.
    source_allowlist_path: str = "config/sources.yaml"
    security_config_path: str = "config/security.yaml"
    auth_mode: AuthMode = "none"
    bearer_token: str | None = None
    # Per-IP rate limits (Phase 8 #79). In-process token bucket;
    # operationally cheap, single-instance only. Horizontal scale
    # needs a shared backend — separate follow-up.
    rate_limit_search_per_min: int = 60
    rate_limit_feedback_per_min: int = 30
    # Honor X-Forwarded-For for rate-limit keying. True in CF (the
    # gorouter strips/sets this header reliably). False elsewhere so a
    # local client can't bypass the limiter by spoofing XFF.
    trust_forwarded_for: bool = False

    # Telemetry.
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "cf-knowledge-kiln"

    # #198: upper bound on the one-shot embedding-provider health probe
    # run at startup. 90 s gives ~3x the previous 30 s headroom for
    # cold first-call HuggingFace weight pulls, while leaving a 30 s
    # margin under the CF app startup ``timeout: 120`` declared in
    # manifest.yml (so the lifespan can finish the rest of its work —
    # DB pool, config parse, rate limiters — even when the probe burns
    # its full budget). Operators with very large models or slow links
    # bump via ``KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS`` *and* the
    # manifest's ``timeout`` together. Pre-warming the model before the
    # first start sidesteps the whole timed path — see
    # ``docs/deployment-cloud-foundry.md``.
    embedding_probe_timeout_seconds: float = 90.0

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
