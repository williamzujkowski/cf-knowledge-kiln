"""FastAPI application factory and core routes.

This file intentionally stays small. Routers under
``cf_knowledge_kiln.api.routers`` are added as features land in later
phases (Phase 5 for retrieval, Phase 7 for CF readiness probes that
verify service bindings, and so on).
"""

from __future__ import annotations

from fastapi import FastAPI

from cf_knowledge_kiln import __version__
from cf_knowledge_kiln.api.health import router as health_router
from cf_knowledge_kiln.config import get_settings


def create_app() -> FastAPI:
    """Build a FastAPI app instance.

    A factory rather than a module-level constant so tests can build
    fresh apps with isolated settings.
    """
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Cloud Foundry-ready RAG knowledge substrate. "
            "Hybrid retrieval over internal documentation; cited human "
            "results and bounded agent context packs."
        ),
        # OpenAPI 3.1 is the default in recent FastAPI; we pin the
        # schema URL to match the file we hand-author for the spec gate.
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.include_router(health_router)
    return app


app = create_app()
