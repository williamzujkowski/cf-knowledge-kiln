"""FastAPI dependency providers for Phase 5 routes.

The lifespan in :mod:`cf_knowledge_kiln.api.app` attaches the shared
:class:`Database`, :class:`EmbeddingProvider`, and (later) other
services to ``app.state``. These ``Depends``-able functions hand them
to route handlers without each handler reaching into ``request.app``
manually.

The handlers raise HTTP 503 when a required dependency is missing
(e.g., no DB binding) so operators see "service degraded" rather
than a 500.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.rate_limit import TokenBucketLimiter
from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider
from cf_knowledge_kiln.retrieval import (
    HybridRetriever,
    RetrievalConfig,
    load_retrieval_config,
)


def get_db(request: Request) -> Database:
    """Return the live :class:`Database` from app.state or 503."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not configured; bind a Postgres service or set KILN_DATABASE_URL.",
        )
    assert isinstance(db, Database)
    return db


async def get_session(
    db: Annotated[Database, Depends(get_db)],
) -> AsyncIterator[AsyncSession]:
    """Yield ONE session + transaction per request (issue #74).

    Both retrieval and the per-request telemetry write share this
    session. FastAPI calls the dependency once per request; the
    ``yield`` form ensures the transaction commits on success and
    rolls back on a handler exception.
    """
    async with db.session() as session, session.begin():
        yield session


def get_embedding_provider(request: Request) -> EmbeddingProvider | None:
    """Return the optional :class:`EmbeddingProvider` from app.state.

    ``None`` is a valid state: FTS-only retrieval still works without
    embeddings. Handlers don't need to 503 on this.
    """
    provider = getattr(request.app.state, "embedding_provider", None)
    if provider is None:
        return None
    return provider  # type: ignore[no-any-return]


def get_retrieval_config(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RetrievalConfig:
    """Load :class:`RetrievalConfig` from ``config/security.yaml``.

    The loader returns defaults + a warning when the file is missing —
    so this dep never raises for a missing config.
    """
    return load_retrieval_config(settings.security_config_path)


def get_hybrid_retriever(
    db: Annotated[Database, Depends(get_db)],
    provider: Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)],
    config: Annotated[RetrievalConfig, Depends(get_retrieval_config)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HybridRetriever:
    """Compose a per-request :class:`HybridRetriever`.

    The retriever is cheap to construct (no I/O); building per-request
    keeps it stateless and avoids accidentally sharing transaction
    state across concurrent calls.
    """
    return HybridRetriever(
        db=db,
        embedding_provider=provider,
        config=config,
        ef_search=settings.hnsw_ef_search,
    )


def get_search_limiter(request: Request) -> TokenBucketLimiter:
    """Per-IP rate limiter for /v1/search and /search (#79)."""
    limiter = getattr(request.app.state, "search_limiter", None)
    if limiter is None:
        # Lifespan ran without setting state — should never happen
        # outside test fixtures that build a bare app.
        raise HTTPException(status_code=500, detail="rate limiter not initialized")
    assert isinstance(limiter, TokenBucketLimiter)
    return limiter


def get_feedback_limiter(request: Request) -> TokenBucketLimiter:
    """Per-IP rate limiter for /feedback (#79)."""
    limiter = getattr(request.app.state, "feedback_limiter", None)
    if limiter is None:
        raise HTTPException(status_code=500, detail="rate limiter not initialized")
    assert isinstance(limiter, TokenBucketLimiter)
    return limiter


def get_trust_xff(
    settings: Annotated[Settings, Depends(get_settings)],
) -> bool:
    """Whether to honor X-Forwarded-For for client-IP keying (#79).

    Default off; flip on in CF where the gorouter sets XFF reliably.
    """
    return settings.trust_forwarded_for


__all__ = [
    "get_db",
    "get_embedding_provider",
    "get_feedback_limiter",
    "get_hybrid_retriever",
    "get_retrieval_config",
    "get_search_limiter",
    "get_session",
    "get_trust_xff",
]
