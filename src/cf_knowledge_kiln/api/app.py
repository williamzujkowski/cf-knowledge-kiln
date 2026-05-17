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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cf_knowledge_kiln import __version__
from cf_knowledge_kiln.api.health import router as health_router
from cf_knowledge_kiln.api.retrieval import router as retrieval_router
from cf_knowledge_kiln.config import get_settings
from cf_knowledge_kiln.db import Database, resolve_database_url
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.factory import build_provider_from_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the async DB pool + embedding provider around the app's lifetime."""
    settings = get_settings()
    url = resolve_database_url(settings)
    db: Database | None = None
    if url is not None:
        db = Database(
            url,
            pool_size=settings.pg_pool_size,
            max_overflow=settings.pg_pool_max_overflow,
        )
    embedding_provider: EmbeddingProvider | None = build_provider_from_settings(settings)
    app.state.db = db
    app.state.embedding_provider = embedding_provider
    try:
        yield
    finally:
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
    app.include_router(health_router)
    app.include_router(retrieval_router)
    return app


app = create_app()
