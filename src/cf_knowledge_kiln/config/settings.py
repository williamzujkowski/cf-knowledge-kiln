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
# #315: ``oidc`` mode adds browser SSO + agent JWT bearer alongside
# the existing static-bearer / mtls / none modes.
AuthMode = Literal["none", "bearer", "mtls", "oidc"]


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
    # #244: auto-apply Alembic migrations at app startup. Default ON
    # so a fresh ``cf push`` against an empty DB just works — the API
    # and worker run ``alembic upgrade head`` against the resolved DB
    # URL before opening their connection pool. A Postgres
    # transaction-level advisory lock serializes concurrent starts.
    # Operators with shared-DB deployments where another process owns
    # the schema (or who prefer the explicit ``make migrate`` flow)
    # set this to ``false`` and migrate out-of-band.
    auto_migrate_on_startup: bool = True

    # Embedding provider — see config/models.yaml for the swappable registry.
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None

    # Optional generator (for /v1/answer).
    generator_api_key: str | None = None
    generator_base_url: str | None = None

    # #332 / #333: HyDE query expansion. ``hyde_enabled`` is the master
    # switch; the other six tune behavior when on. ``hyde_enabled=true``
    # with no generator configured is a no-op (one INFO log at startup;
    # search behaves exactly as if the flag were off). See
    # docs/configuration.md HyDE section for tuning guidance.
    hyde_enabled: bool = False
    hyde_query_token_threshold: int = 8
    hyde_jargon_density_threshold: float = 0.4
    hyde_cache_max_entries: int = 1000
    hyde_cache_ttl_seconds: int = 86400
    hyde_generator_max_tokens: int = 200
    hyde_generator_timeout_seconds: float = 3.0

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
    # #253: private-repo ingestion. All four default to off / safe —
    # public-repo deployments don't set anything. See
    # docs/deployment-cloud-foundry.md#private-git-sources.
    #   git_token: GitHub PAT injected via GIT_ASKPASS at clone time.
    #     Scoped to github.com URLs only.
    #   git_ssh_private_key: raw PEM or base64-encoded PEM. Worker
    #     writes ~/.ssh/id_rsa (0600) at startup.
    #   git_ssh_known_hosts: operator-supplied known_hosts. REPLACES
    #     the bundled GitHub entries when set.
    #   git_ssh_strict_host_key_checking: leave true in production.
    git_token: str | None = None
    git_ssh_private_key: str | None = None
    git_ssh_known_hosts: str | None = None
    git_ssh_strict_host_key_checking: bool = True

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

    # #315: OIDC SSO. Active only when KILN_AUTH_MODE=oidc; the
    # middleware reads issuer metadata at startup and caches the JWKS
    # for token-signature verification. Browser users go through the
    # authorization-code + PKCE flow; agent users send an
    # ``Authorization: Bearer <token>`` they obtained from the same
    # issuer (or, when ``oidc_allow_bearer_fallback`` is true, a static
    # service-account token compared against ``bearer_token``).
    #
    # All values are read via the existing ``KILN_`` env prefix so an
    # operator wires them the same way as every other kiln setting.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    # ``aud`` claim required on inbound tokens. Defaults to client_id
    # at validation time when unset.
    oidc_audience: str | None = None
    # Optional comma-separated list — if any group in this list is
    # present in the token's ``groups`` claim the request is allowed.
    # Empty / unset means group-membership is not enforced.
    oidc_required_groups: str | None = None
    # Claim mapped to ``request.state.username`` and persisted on
    # rag_queries.requester. Defaults to the IdP convention.
    oidc_username_claim: str = "preferred_username"
    # If true, requests that arrive with ``Authorization: Bearer
    # <token>`` may use the static ``bearer_token`` OR a JWT issued by
    # the OIDC issuer. Used to let service accounts coexist with
    # browser-SSO users in a single deployment.
    oidc_allow_bearer_fallback: bool = False
    # Signs the session cookie. Defaults to a runtime-generated key
    # with a log warning — operators running multiple kiln instances
    # behind a single hostname MUST set this to a stable shared secret
    # or browser sessions will be invalidated on every reschedule.
    oidc_session_secret: str | None = None
    # The path under which kiln registers the OIDC callback. Must
    # match the redirect_uri configured in the IdP for this client.
    oidc_redirect_path: str = "/auth/callback"

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

    # #314: optional URL to the agent integration guide, surfaced as a
    # "Agents → /v1/agent/context-pack" link in the colophon so a
    # visiting engineer discovers the agent endpoint surface (UX-audit
    # finding: undiscoverable from the human UI). Default-None means
    # the link is omitted entirely on stock deployments — fork
    # operators set this to their published guide URL (typically the
    # GitHub blob URL of docs/agent-integration-guide.md).
    agent_guide_url: str | None = None

    # #359: TTL for the GET /v1/registry cache. Agents bootstrapping
    # a kiln client call /v1/registry once to learn the per-dimension
    # filter vocabulary, then cache it locally; the kiln's own
    # process-local cache avoids re-aggregating the documents table
    # for every bootstrap call. 5 min default keeps the response
    # within one ingestion cycle of fresh; operators with bursty
    # ingestion can drop it via KILN_REGISTRY_CACHE_SECONDS=60.
    registry_cache_seconds: int = 300

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

    @property
    def oidc_required_groups_list(self) -> list[str]:
        """Parse :attr:`oidc_required_groups` into a list of group names.

        Empty / unset returns ``[]`` (group enforcement off). Whitespace
        around commas is trimmed; empty entries are dropped — operators
        get the obvious behavior when they write
        ``KILN_OIDC_REQUIRED_GROUPS="admins, , kiln-users"``.
        """
        raw = self.oidc_required_groups or ""
        return [s.strip() for s in raw.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance.

    Tests that need a fresh settings instance should call
    ``get_settings.cache_clear()`` first.
    """
    return Settings()
