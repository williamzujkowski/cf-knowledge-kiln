"""GET /v1/registry — filter vocabulary surface (#359).

An agent that bootstraps a kiln client can call this once to learn
the per-dimension filter vocabulary in the current deploy. Without
this surface the agent has to either:

* Send filter values that the kiln may not recognize, and parse
  the resulting ``invalid_filter_value`` error, OR
* Send filter values that the kiln DOES recognize but that have
  zero indexed documents, and get an empty ``evidence`` array
  indistinguishable from a "no matching docs" answer.

This module wraps :class:`RegistryRepository` with an in-process
TTL cache so a high-QPS bootstrap callsite doesn't re-aggregate the
``documents`` table on every request. Cache TTL is config-driven
via ``KILN_REGISTRY_CACHE_SECONDS`` (default 300 = 5 min).

Per AGENTS.md: read-only; same auth requirements as the other
``/v1/agent/*`` endpoints (governed by the auth middleware, not
this route). The cache is per-process — multi-instance deployments
each cache independently, which is fine for a 5-min stale window.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.dependencies import get_session
from cf_knowledge_kiln.config import get_settings
from cf_knowledge_kiln.db.repositories import RegistryRepository
from cf_knowledge_kiln.retrieval.types import (
    RegistryDimension,
    RegistryResponse,
    RegistryValue,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])


# Process-local cache. The kiln runs a single uvicorn worker per CF
# instance in the default manifest, so a process-local cache is the
# right scope. For multi-worker / multi-instance setups each worker
# warms its own cache; a 5-min window means the worst case is a
# 5-min stale aggregate which is fine for a "what filter values
# exist?" query.
_cache_built_at: datetime | None = None
_cache_payload: RegistryResponse | None = None


def _ttl_expired(now: datetime) -> bool:
    """True iff the cache is empty or older than the configured TTL."""
    if _cache_built_at is None or _cache_payload is None:
        return True
    ttl_s = get_settings().registry_cache_seconds
    age = (now - _cache_built_at).total_seconds()
    return age >= ttl_s


def _reset_cache_for_tests() -> None:
    """Test-only seam — pinning the TTL is hard if the cache is
    process-global with no way to clear. Reset between tests.
    """
    global _cache_built_at, _cache_payload
    _cache_built_at = None
    _cache_payload = None


async def _build_response(session: AsyncSession) -> RegistryResponse:
    """Aggregate the registry once + serialize to the wire shape."""
    repo = RegistryRepository(session)
    rows_by_dim = await repo.aggregate_all()
    dimensions: dict[str, list[RegistryValue]] = {
        dim: [
            RegistryValue(value=r.value, count=r.count, last_indexed=r.last_indexed) for r in rows
        ]
        for dim, rows in rows_by_dim.items()
    }
    return RegistryResponse(dimensions=dimensions, as_of=datetime.now(UTC))


@router.get(
    "/v1/registry",
    operation_id="agentRegistry",
    summary="Filter vocabulary for the current deploy",
    response_model=RegistryResponse,
    response_model_exclude_none=True,
    responses={
        status.HTTP_200_OK: {"description": "Per-dimension filter vocabulary."},
    },
)
async def registry(
    session: Annotated[AsyncSession, Depends(get_session)],
    dimension: RegistryDimension | None = None,
) -> RegistryResponse:
    """Return the per-dimension filter vocabulary.

    Aggregates ``documents`` table state into per-dimension value
    lists. Each entry carries ``count`` (how many docs have it) and
    ``last_indexed`` (most-recent ``last_reviewed`` date in the
    bucket). Cached for ``KILN_REGISTRY_CACHE_SECONDS`` (default
    300) per process.

    Pass ``?dimension=<name>`` to limit the response to a single
    dimension; without it every supported dimension is returned.
    """
    global _cache_built_at, _cache_payload
    now = datetime.now(UTC)
    if _ttl_expired(now):
        _cache_payload = await _build_response(session)
        _cache_built_at = now
    assert _cache_payload is not None  # _build_response just populated it
    if dimension is None:
        return _cache_payload
    # Single-dimension shape: return the full response shell but with
    # only the requested dimension. Cleaner than a separate response
    # model for the one-key case + lets the consumer use the same
    # parsing code either way.
    return RegistryResponse(
        dimensions={dimension: _cache_payload.dimensions.get(dimension, [])},
        as_of=_cache_payload.as_of,
    )


__all__ = ["router"]
