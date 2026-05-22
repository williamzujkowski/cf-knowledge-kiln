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


def get_retrieval_config(request: Request) -> RetrievalConfig:
    """Return the retrieval config, parsed once at app startup (#183).

    The lifespan parses ``config/security.yaml`` into
    ``app.state.retrieval_config`` so the retrieval hot path doesn't
    re-read + re-parse the file on every request. The fallback re-loads
    only when state is unset — an app constructed without entering its
    lifespan, which happens in some bare-app test setups.
    """
    config = getattr(request.app.state, "retrieval_config", None)
    if isinstance(config, RetrievalConfig):
        return config
    return load_retrieval_config(get_settings().security_config_path)


def get_prompt_injection_phrases(request: Request) -> list[str]:
    """Return the prompt-injection phrase list, loaded once at startup (#183).

    Same rationale as :func:`get_retrieval_config`: the lifespan loads
    the list into ``app.state`` so query normalization doesn't re-read
    ``config/security.yaml`` per request. The fallback covers a
    no-lifespan bare app.
    """
    phrases = getattr(request.app.state, "prompt_injection_phrases", None)
    if isinstance(phrases, list):
        return phrases
    from cf_knowledge_kiln.ingestion.prompt_injection import load_phrases

    return load_phrases(get_settings().security_config_path)


def get_hybrid_retriever(
    db: Annotated[Database, Depends(get_db)],
    provider: Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)],
    config: Annotated[RetrievalConfig, Depends(get_retrieval_config)],
    phrases: Annotated[list[str], Depends(get_prompt_injection_phrases)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HybridRetriever:
    """Compose a per-request :class:`HybridRetriever`.

    The retriever is cheap to construct (no I/O); building per-request
    keeps it stateless and avoids accidentally sharing transaction
    state across concurrent calls. Its expensive inputs — the retrieval
    config and the #100 prompt-injection phrase list — are loaded once
    at startup and read from ``app.state`` (#183), so only the
    lightweight object assembly happens per request.
    """
    return HybridRetriever(
        db=db,
        embedding_provider=provider,
        config=config,
        ef_search=settings.hnsw_ef_search,
        prompt_injection_phrases=phrases,
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
    "get_prompt_injection_phrases",
    "get_retrieval_config",
    "get_search_limiter",
    "get_session",
    "get_trust_xff",
]
