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

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from fastapi import HTTPException, Request, status

# Cap the per-key bucket dict so a spray of distinct keys (e.g. random
# X-Forwarded-For values) can't grow it without bound. 50k entries is
# generous for any plausible single-instance deployment; eviction is
# strictly oldest-first, which is fine because an evicted key just
# resets to full capacity on its next hit (worst case: one free token
# for an attacker that already spent theirs).
_MAX_BUCKETS = 50_000


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

    def __init__(
        self,
        *,
        capacity: int,
        window_seconds: float,
        max_buckets: int = _MAX_BUCKETS,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        if max_buckets <= 0:
            raise ValueError(f"max_buckets must be positive, got {max_buckets}")
        self._capacity = float(capacity)
        self._window = float(window_seconds)
        # At default 60/60 this is 1 token/sec, which is also the
        # smallest deficit retry_after can report.
        self._refill_per_sec = self._capacity / self._window
        # OrderedDict + move_to_end gives us O(1) LRU eviction.
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._max_buckets = max_buckets
        self._lock = Lock()

    def hit(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token. Returns True on allow, False on deny."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last_refill=t)
                self._buckets[key] = bucket
                # Evict the oldest bucket if we're over budget. Worst
                # case: an evicted attacker gets a fresh full bucket
                # on their next hit — bounded by the eviction rate.
                if len(self._buckets) > self._max_buckets:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
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
            return max(1, math.ceil(deficit / self._refill_per_sec))


def _normalize_ip(raw: str) -> str:
    """Strip brackets/whitespace and case-fold IPv6 so the key is canonical."""
    s = raw.strip()
    # IPv6-in-brackets ("[::1]" / "[::1]:port") — drop brackets and any port.
    if s.startswith("["):
        end = s.find("]")
        if end != -1:
            s = s[1:end]
    return s.casefold()


def client_ip(request: Request, *, trust_xff: bool = False) -> str:
    """Best-effort client IP for rate-limit keying.

    Honors ``X-Forwarded-For`` only when ``trust_xff`` is set (the CF
    gorouter strips/sets it reliably, but a direct caller can spoof
    it). The first XFF entry is treated as the original client.
    Returns a normalized lowercase string with IPv6 brackets stripped.

    **Do not** use this for security decisions — XFF is operator-
    controllable for unauthenticated callers even when ``trust_xff``
    is True.
    """
    if trust_xff:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",", 1)[0]
            return _normalize_ip(first)
    if request.client is not None:
        return _normalize_ip(request.client.host)
    return "unknown"


def raise_429_if_limited(
    limiter: TokenBucketLimiter, request: Request, *, trust_xff: bool = False
) -> None:
    """Standard 429 raise for the JSON-API routes.

    HTML/HTMX routes that want a fragment instead of a JSON body call
    ``limiter.hit()`` directly and render their own response.
    """
    key = client_ip(request, trust_xff=trust_xff)
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
