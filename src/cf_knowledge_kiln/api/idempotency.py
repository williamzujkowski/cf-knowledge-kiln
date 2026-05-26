"""Idempotency-Key dispatch (#309).

Implements the Stripe-style contract for agent POST endpoints.
Honored on ``/v1/search``, ``/v1/agent/context-pack``,
``/v1/answer``.

Contract:

* Inbound ``Idempotency-Key`` header (alphanumeric +
  ``._-``, max 200 chars) — sanitized via
  :func:`cf_knowledge_kiln.api._header_sanitize.sanitize_opaque_header`.
* First request with a given key for a given route caches the
  response body + status in ``idempotency_keys``.
* Retry with the same key + same body → byte-identical replay
  (``Idempotency-Replayed: true`` header). No retriever
  invocation, no telemetry write, no rate-limit token burn.
* Retry with the same key + DIFFERENT body → 422
  ``idempotency_conflict``. Mirrors Stripe; distinguishes
  "legitimate retry" from "agent changed its mind mid-retry."
* Missing header → current non-idempotent behavior.

Cache 2xx + 4xx (validation errors, conflicts). Do NOT cache
5xx ``retry_safe`` responses — those are transient and the
contract says "try again."

Lives in its own module (not as a FastAPI middleware) because
the dispatcher needs the PARSED Pydantic body to canonicalize
+ hash, which middleware can't access without re-reading the
request stream. The handlers call ``check_or_replay`` after
Pydantic validation, ``store`` after producing the response.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api._header_sanitize import sanitize_opaque_header
from cf_knowledge_kiln.db.repositories.idempotency import (
    IdempotencyRepository,
)

logger = logging.getLogger(__name__)

# Standard header name per draft-ietf-httpapi-idempotency-key /
# Stripe / industry consensus.
HEADER = "Idempotency-Key"

# Replays carry this response header so an agent's tooling can
# detect replay vs fresh execution. Value is always "true" when
# present.
REPLAY_HEADER = "Idempotency-Replayed"


class Outcome(StrEnum):
    """Dispatcher decision."""

    # Header absent or unusable — proceed with normal handler flow.
    MISS = "miss"
    # Header present, row found, body matches — replay the
    # cached response.
    HIT = "hit"
    # Header present, row found, body MISMATCHES — caller is
    # retrying with a different body under the same key. Raise
    # 422 idempotency_conflict.
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CheckResult:
    """Result of the pre-handler check_or_replay call.

    On MISS, ``key`` is the sanitized header (or None if header
    absent); the handler runs normally then calls ``store`` with
    the same key + the produced response.

    On HIT, ``cached_body`` + ``cached_status`` are the bytes
    to re-serve.

    On CONFLICT, the handler raises 422 — no cached body needed.
    """

    outcome: Outcome
    key: str | None
    request_hash: str | None
    cached_body: dict[str, Any] | None
    cached_status: int | None


def canonical_body_hash(body: dict[str, Any] | None) -> str:
    """SHA-256 of the body with keys recursively sorted.

    JSON-key order doesn't matter for the request semantically,
    so the hash MUST be order-independent. ``json.dumps`` with
    ``sort_keys=True`` recurses into nested dicts; tuples in
    Pydantic models are already sorted by attribute name at
    .model_dump() time, but we re-sort defensively here.

    ``None`` body (GET-without-body — unused on the protected
    POST endpoints today, but harmless) hashes to a stable
    empty-object value so the dispatcher can rely on a string
    return.
    """
    payload = body if body is not None else {}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def extract_key(request: Request) -> str | None:
    """Read the Idempotency-Key header and sanitize.

    Returns ``None`` when:
    * the header is absent (most common — agent didn't opt in),
    * the header is empty / all whitespace,
    * the sanitized form is empty (all chars stripped — caller
      sent a hostile / nonsense value, treat as absent rather
      than producing an all-underscore cache key).
    """
    raw = request.headers.get(HEADER)
    return sanitize_opaque_header(raw)


async def check_or_replay(
    *,
    session: AsyncSession,
    request: Request,
    route: str,
    body: dict[str, Any] | None,
) -> CheckResult:
    """Pre-handler dispatch. Call AFTER Pydantic validation.

    Returns a :class:`CheckResult`:

    * On ``MISS``: handler runs normally; if ``key`` is non-None
      the handler must call :func:`store` with the response.
    * On ``HIT``: handler should short-circuit and return the
      cached response (the caller does the actual return; this
      function doesn't touch FastAPI's response object).
    * On ``CONFLICT``: handler raises
      ``raise_with_code(422, "idempotency_conflict")``.

    The ``route`` arg is the path key for the per-route lookup
    (e.g. ``"/v1/agent/context-pack"``). Pass a stable string;
    do NOT use ``request.url.path`` because that includes
    query strings on some FastAPI setups.
    """
    key = extract_key(request)
    if key is None:
        return CheckResult(Outcome.MISS, None, None, None, None)

    request_hash = canonical_body_hash(body)
    repo = IdempotencyRepository(session)
    cached = await repo.lookup(key=key, route=route)
    if cached is None:
        return CheckResult(Outcome.MISS, key, request_hash, None, None)

    # Body mismatch under the same key → conflict. The retry
    # path (which the Idempotency-Key contract is FOR) carries
    # the same body by definition, so any mismatch is the agent
    # changing its mind mid-retry — fail loudly so the operator
    # notices.
    if cached.request_hash != request_hash:
        return CheckResult(Outcome.CONFLICT, key, request_hash, None, None)

    return CheckResult(
        Outcome.HIT,
        key,
        request_hash,
        dict(cached.response_body) if cached.response_body else {},
        int(cached.response_status),
    )


# 5xx responses that the envelope marks ``retry_safe: true`` are
# NOT cached — the contract says retrying is expected to succeed,
# so caching the failure would defeat the contract. 4xx + 200 are
# cached uniformly. 503 generator_unavailable is specifically NOT
# cached (configuration issue — operator must intervene).
_CACHE_STATUS_RANGE_MIN = 200
_CACHE_STATUS_RANGE_MAX = 499  # inclusive


def should_cache(status: int) -> bool:
    """True iff the response status should be cached for replay.

    2xx + 4xx → True. 5xx → False (transient or operator-
    intervention path; replay would be wrong).
    """
    return _CACHE_STATUS_RANGE_MIN <= status <= _CACHE_STATUS_RANGE_MAX


async def store(
    *,
    session: AsyncSession,
    key: str,
    route: str,
    request_hash: str,
    resource_id: str | None,
    response_body: dict[str, Any],
    response_status: int,
) -> None:
    """Post-handler cache write. Call after the handler produces
    its response, inside a session.begin_nested() savepoint so a
    failed cache-store doesn't 500 the response.

    The handler must already have done the
    ``should_cache(response_status)`` check — this function
    unconditionally inserts. Separating the cache-vs-not decision
    from the write keeps the dispatcher dead-simple and the
    should_cache policy in one place.
    """
    try:
        async with session.begin_nested():
            await IdempotencyRepository(session).create(
                key=key,
                route=route,
                request_hash=request_hash,
                resource_id=resource_id,
                response_body=response_body,
                response_status=response_status,
            )
    except Exception:
        # Telemetry-style non-fatal: a cache-write failure must
        # not turn a successful handler response into a 500.
        # Matches the pattern in api/views.log_human_query and
        # api/retrieval._log_context_pack.
        logger.exception("idempotency_keys cache write failed (non-fatal)")


__all__ = [
    "HEADER",
    "REPLAY_HEADER",
    "CheckResult",
    "Outcome",
    "canonical_body_hash",
    "check_or_replay",
    "extract_key",
    "should_cache",
    "store",
]
