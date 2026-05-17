"""In-process per-IP rate limiter (Phase 8 hardening, issue #79).

A small token-bucket gate intended for two routes that take real DB
work per request: ``POST /v1/search`` and ``POST /feedback``. The
limiter is deliberately in-process — operationally cheap, single-
instance only. Horizontal scale will need a shared backend (Redis or
similar); that's a separate follow-up.

Defaults (overridable via env):

* ``KILN_RATE_LIMIT_SEARCH_PER_MIN`` (default 60) — per-IP cap for
  ``POST /v1/search`` and ``POST /search`` (the HTMX form posts on
  every keystroke, debounced; 60/min is comfortable for a human but
  catches a tight scripted loop).
* ``KILN_RATE_LIMIT_FEEDBACK_PER_MIN`` (default 30) — per-IP cap
  for ``POST /feedback``. Lower because each call writes a row.

Returns ``429 Too Many Requests`` with ``Retry-After: <seconds>``
when a bucket is empty. HTMX-friendly: callers that want a swappable
fragment instead of a raw 429 body wrap the response themselves.

Per AGENTS.md: this is operator-policy defense in depth, not a
substitute for upstream rate-limiting at the CF gorouter / CDN.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

from fastapi import HTTPException, Request, status


@dataclass
class _Bucket:
    """Single token bucket with monotonic refill."""

    tokens: float
    last_refill: float


class TokenBucketLimiter:
    """Per-key token-bucket rate limiter.

    ``key`` is whatever the caller wants to limit on (typically a
    client IP). The bucket holds ``capacity`` tokens; tokens refill
    linearly at ``capacity`` tokens per ``window_seconds``. A request
    that arrives with 0 tokens is rejected.

    Process-local. Memory grows linearly with the number of distinct
    keys seen — fine for a single-instance dev/MVP, sized for ~10k
    distinct IPs without thinking about it. If that becomes a
    concern, swap in an LRU at the dict layer.
    """

    def __init__(self, *, capacity: int, window_seconds: float) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        self._capacity = float(capacity)
        self._window = float(window_seconds)
        self._refill_per_sec = self._capacity / self._window
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def hit(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token. Returns True on allow, False on deny."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last_refill=t)
                self._buckets[key] = bucket
            elapsed = max(0.0, t - bucket.last_refill)
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_sec)
            bucket.last_refill = t
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Seconds until the bucket regenerates one token. Always ≥1."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # No prior request → no wait needed, but return 1 so
                # the header is always at least "Retry-After: 1".
                return 1
            elapsed = max(0.0, t - bucket.last_refill)
            tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_sec)
            if tokens >= 1.0:
                return 1
            deficit = 1.0 - tokens
            return max(1, int(deficit / self._refill_per_sec) + 1)


def client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit keying.

    Honors ``X-Forwarded-For`` (CF gorouter sets it) — falls back to
    the immediate peer. The first XFF entry is treated as the original
    client. **Do not** use this for security decisions — XFF is
    client-controllable for unauthenticated callers.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def raise_429_if_limited(limiter: TokenBucketLimiter, request: Request) -> None:
    """Standard 429 raise for the JSON-API routes.

    HTML/HTMX routes that want a fragment instead of a JSON body call
    ``limiter.hit()`` directly and render their own response.
    """
    key = client_ip(request)
    if not limiter.hit(key):
        retry = limiter.retry_after(key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={"Retry-After": str(retry)},
        )


__all__ = [
    "TokenBucketLimiter",
    "client_ip",
    "raise_429_if_limited",
]
