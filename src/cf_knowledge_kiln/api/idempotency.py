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
from datetime import UTC, datetime
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


# Wire-level invariant: an Idempotency-Key MUST fit the DB column
# (TEXT, but the OpenAPI schema declares ``maxLength: 200``) and
# MUST be ≤ the sanitizer's truncation cap. The sanitizer already
# enforces the truncation, but pinning it explicitly here gives a
# future refactor a single canonical constant to grep for if the
# limit moves. Belt-and-braces against a hostile sanitizer-bypass
# (blind-review #320 finding).
MAX_KEY_LEN = 200


def extract_key(request: Request) -> str | None:
    """Read the Idempotency-Key header and sanitize.

    Returns ``None`` when:
    * the header is absent (most common — agent didn't opt in),
    * the header is empty / all whitespace,
    * the sanitized form is empty (all chars stripped — caller
      sent a hostile / nonsense value, treat as absent rather
      than producing an all-underscore cache key).

    Defensive invariant: the returned key MUST be at most
    :data:`MAX_KEY_LEN` characters. The sanitizer already truncates,
    but a future refactor that loosens the sanitizer must NOT silently
    push the cap. If somehow the post-sanitize value exceeds the cap
    we truncate again rather than failing the request — the alternative
    (raising) would 500 every retry of an otherwise-valid request.
    """
    raw = request.headers.get(HEADER)
    sanitized = sanitize_opaque_header(raw)
    if sanitized is None:
        return None
    if len(sanitized) > MAX_KEY_LEN:
        # Belt-and-braces: if a future sanitizer change ever produces
        # a longer string, log + truncate rather than silently passing
        # an oversized key through to the DB.
        logger.warning(
            "Idempotency-Key sanitized output exceeded MAX_KEY_LEN (%d > %d) — "
            "truncating; check api._header_sanitize for drift.",
            len(sanitized),
            MAX_KEY_LEN,
        )
        return sanitized[:MAX_KEY_LEN]
    return sanitized


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

    # Expiry: rows past their ``expires_at`` must NOT replay, even
    # if the sweeper hasn't reclaimed them yet. The repo's
    # ``lookup()`` deliberately doesn't filter expired rows (so the
    # sweeper itself can find them), so the request-path filter
    # lives here. Treat an expired row as MISS — the handler will
    # re-run, store() will UPSERT a fresh row (or fail-soft on the
    # PK collision, which the next sweeper pass cleans up).
    if cached.expires_at <= datetime.now(UTC):
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

    #320 items 1 + 2: uses ``create_if_absent`` (INSERT … ON
    CONFLICT DO NOTHING) so two concurrent same-key submissions
    don't race-error; the loser silently observes that the winner
    already cached the response. Remaining exceptions are
    discriminated:

    * ``OperationalError`` — transient DB (connection drop,
      timeout). WARNING log; the next retry's check_or_replay will
      MISS and re-execute (correctness preserved; we just didn't
      cache the response).
    * ``IntegrityError`` — genuine schema-level constraint failure
      (NOT the race; that's the DO-NOTHING branch). ERROR log;
      surfaces real corruption to an operator without 500-ing
      the otherwise-successful response.
    * Anything else — ERROR log, opaque to the caller.
    """
    # Local imports keep the module import-light + avoid forcing
    # asyncpg / sqlalchemy.exc onto consumers that only touch the
    # dispatch protocol.
    from sqlalchemy.exc import IntegrityError, OperationalError

    try:
        async with session.begin_nested():
            inserted = await IdempotencyRepository(session).create_if_absent(
                key=key,
                route=route,
                request_hash=request_hash,
                resource_id=resource_id,
                response_body=response_body,
                response_status=response_status,
            )
        if not inserted:
            # Race lost: a sibling request already cached this
            # (key, route). Functionally fine — both handlers
            # produced a valid response; only the winner's bytes
            # are cached. The agent's retry hits the winner's row
            # so the byte-identical-replay contract holds.
            logger.info(
                "idempotency_keys race observed (key=%s route=%s) — "
                "sibling request already cached; this response not cached.",
                key,
                route,
            )
    except OperationalError:
        # Transient DB issue. Telemetry-style non-fatal: a cache-
        # write failure must not turn a successful handler response
        # into a 500. The next retry's check_or_replay will MISS
        # and the handler re-executes — correctness preserved.
        logger.warning(
            "idempotency_keys cache write failed: transient DB error "
            "(key=%s route=%s, response NOT cached, next retry will re-run).",
            key,
            route,
        )
    except IntegrityError:
        # Genuine constraint violation — NOT the (key, route) race
        # (DO-NOTHING handled that). Surface as ERROR so an
        # operator notices real schema corruption.
        logger.exception(
            "idempotency_keys cache write failed: integrity constraint "
            "(key=%s route=%s). Likely schema-level corruption — investigate.",
            key,
            route,
        )
    except Exception:
        # Defense in depth for the rare "neither of the above" path.
        # Same non-fatal contract — the handler response stands.
        logger.exception(
            "idempotency_keys cache write failed (key=%s route=%s, "
            "non-fatal — response NOT cached).",
            key,
            route,
        )


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
