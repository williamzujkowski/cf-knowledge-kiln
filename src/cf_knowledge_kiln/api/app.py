"""FastAPI application factory and lifespan.

The lifespan resolves the database URL (settings or VCAP_SERVICES) and,
if one is configured, builds a :class:`Database` and attaches it to
``app.state.db``. The instance is disposed at shutdown.

If no URL is configured (e.g. local dev without a Postgres), the app
still starts; ``/readyz`` reports ``postgres: failing`` and returns
``status: degraded``. ``/healthz`` is unaffected — liveness is process
liveness, not dependency health.

The same lifespan builds the active :class:`EmbeddingProvider` from
``config/models.yaml`` and attaches it to ``app.state.embedding_provider``.
The local provider lazy-loads ~500 MB of weights, so building it
once per app is the only viable pattern. If no config file is present
the slot is ``None`` and ``/v1/search`` returns 503 until the operator
fixes the config.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cf_knowledge_kiln import __version__
from cf_knowledge_kiln.api.answer import router as answer_router
from cf_knowledge_kiln.api.auth import configure_auth
from cf_knowledge_kiln.api.csp import install_csp_middleware
from cf_knowledge_kiln.api.health import router as health_router
from cf_knowledge_kiln.api.observability import configure_observability
from cf_knowledge_kiln.api.preview import router as preview_router
from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter
from cf_knowledge_kiln.api.request_log import install_request_logging
from cf_knowledge_kiln.api.retrieval import router as retrieval_router
from cf_knowledge_kiln.api.web import router as web_router
from cf_knowledge_kiln.config import get_settings
from cf_knowledge_kiln.db import Database, resolve_database_url
from cf_knowledge_kiln.db.migrations import run_upgrade_head
from cf_knowledge_kiln.generation import GeneratorProvider
from cf_knowledge_kiln.generation.factory import build_generator_from_settings
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.factory import build_provider_from_settings
from cf_knowledge_kiln.ingestion.prompt_injection import load_phrases
from cf_knowledge_kiln.retrieval import load_retrieval_config

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


async def _probe_embedding(provider: EmbeddingProvider | None, *, timeout_seconds: float) -> str:
    """One-shot embedding-provider health check for ``/readyz`` (#176, #198).

    Returns ``"not_configured"`` when no provider is wired (FTS-only is
    a valid mode), ``"ok"`` when a probe embed succeeds, or ``"failing"``
    when it raises or times out. Run once at startup; ``/readyz`` reports
    the cached result rather than re-probing per call — re-probing would
    load the local model / hit the remote API on every poll. A backend
    that fails *after* startup is not caught: that is a documented
    limitation (see #176), acceptable because the common failure is a
    deploy-time misconfiguration (bad URL, missing model, dim mismatch).

    ``timeout_seconds`` is operator-tunable via
    ``KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS`` (#198). Cold HuggingFace
    weight pulls on first start regularly exceeded the previous hardcoded
    30 s bound and left ``/readyz`` stuck at ``embedding: failing`` for
    the life of the process — the default is now 90 s, which gives ~3x
    the original headroom while staying under the manifest's
    ``timeout: 120`` (so the rest of the lifespan still has room).
    Pre-warming the model with a one-line ``encode([\"x\"])`` script
    avoids the timed path entirely.
    """
    if provider is None:
        return "not_configured"
    logger.info(
        "embedding provider health probe starting (timeout=%.0fs); "
        "cold HuggingFace pulls may take a while — pre-warm or bump "
        "KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS if this trips",
        timeout_seconds,
    )
    try:
        await asyncio.wait_for(
            # #204: use embed_query so the probe exercises the same
            # prefix path a real /v1/search query takes — a misconfigured
            # prefix surfaces here at startup instead of as silently
            # bad cosine scores at query time.
            provider.embed_query("readyz embedding health probe"),
            timeout=timeout_seconds,
        )
    except Exception:
        logger.exception(
            "embedding provider health probe failed; /readyz will report degraded. "
            "If this was a cold-cache weight download, pre-warm the model and restart, "
            "or raise KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS (current: %.0fs).",
            timeout_seconds,
        )
        return "failing"
    logger.info("embedding provider health probe ok")
    return "ok"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the async DB pool + embedding provider around the app's lifetime."""
    settings = get_settings()
    url = resolve_database_url(settings)
    db: Database | None = None
    if url is not None:
        # #244: auto-apply migrations BEFORE opening the pool. The
        # pool would otherwise warm up against a missing
        # ``alembic_version`` table on a fresh deploy and the API
        # would 500 every request that touches a real table.
        # Migration failure crashes the lifespan — that's intentional;
        # running against an unmigrated DB is worse than not running.
        if settings.auto_migrate_on_startup:
            logger.info(
                "auto-migrate enabled; running alembic upgrade head before opening pool"
            )
            await run_upgrade_head(url)
        db = Database(
            url,
            pool_size=settings.pg_pool_size,
            max_overflow=settings.pg_pool_max_overflow,
        )
    embedding_provider: EmbeddingProvider | None = build_provider_from_settings(settings)
    # #192: optional generator for /v1/answer. ``None`` is the MVP
    # default — get_generator_provider raises 503 in that case so
    # /v1/answer reports "no generator configured" clearly. Other
    # endpoints (/v1/search, /v1/agent/context-pack) ignore this.
    generator_provider: GeneratorProvider | None = build_generator_from_settings(settings)
    app.state.db = db
    app.state.embedding_provider = embedding_provider
    app.state.generator_provider = generator_provider
    # Probe the provider once so /readyz reflects embedding health, not
    # just Postgres (#176). A configured-but-broken provider (e.g. a URL
    # typo) builds an object that only fails on use — this surfaces it.
    app.state.embedding_status = await _probe_embedding(
        embedding_provider,
        timeout_seconds=settings.embedding_probe_timeout_seconds,
    )
    # #183: parse config/security.yaml ONCE at startup. Before this,
    # every /v1/search + /search request re-read and re-parsed the file
    # twice (retrieval config + prompt-injection phrases) — synchronous
    # file I/O on the event loop. The retrieval dependencies now read
    # these from app.state. A malformed security.yaml now fails the
    # deploy at startup rather than 500-ing every request.
    app.state.retrieval_config = load_retrieval_config(settings.security_config_path)
    app.state.prompt_injection_phrases = load_phrases(settings.security_config_path)
    # #79: in-process per-IP rate limiters. Built once per app so the
    # token buckets persist across requests. Two separate limiters
    # because /search and /feedback have different cost profiles.
    app.state.search_limiter = TokenBucketLimiter(
        capacity=settings.rate_limit_search_per_min, window_seconds=60.0
    )
    app.state.feedback_limiter = TokenBucketLimiter(
        capacity=settings.rate_limit_feedback_per_min, window_seconds=60.0
    )
    try:
        yield
    finally:
        if generator_provider is not None:
            await generator_provider.aclose()
        if embedding_provider is not None:
            await embedding_provider.aclose()
        if db is not None:
            await db.dispose()


def create_app() -> FastAPI:
    """Build a FastAPI app instance."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Cloud Foundry-ready RAG knowledge substrate. "
            "Hybrid retrieval over internal documentation; cited human "
            "results and bounded agent context packs."
        ),
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    # Auth middleware is configured BEFORE routers are included so it
    # wraps every endpoint. configure_auth raises at startup for
    # obviously-wrong configs (none-in-prod, bearer-without-token).
    configure_auth(app, settings)
    # #144: strict CSP. Installed after auth so the header is added on
    # every response — including 401 / 429 bodies a browser will still
    # render. Rate-limit is a per-route Depends, not an ASGI
    # middleware, so there's nothing else to sequence against.
    install_csp_middleware(app)
    # #178: per-request observability. Installed last so it is the
    # outermost middleware — the logged duration covers the whole
    # request, including auth + CSP.
    install_request_logging(app)
    # OpenTelemetry tracing — no-op unless KILN_OTEL_EXPORTER_OTLP_ENDPOINT
    # is set AND the [otel] extra is installed. See api/observability.py.
    configure_observability(app, settings)
    app.include_router(health_router)
    app.include_router(retrieval_router)
    app.include_router(answer_router)
    app.include_router(web_router)
    app.include_router(preview_router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    return app


app = create_app()
