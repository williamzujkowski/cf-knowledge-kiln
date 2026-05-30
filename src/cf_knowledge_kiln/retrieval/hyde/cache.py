"""#332 — HyDE pseudo-doc cache. TTL + LRU eviction. In-process only.

Same query → same pseudo-doc until the entry ages out. The cache is
keyed on ``(provider, model, normalized_query)`` so a generator-swap
or a model-rotation invalidates without an explicit flush.

In-process, single-worker: no Redis, no cross-process sharing. The
worker process is single-tenant per CF instance and HyDE expansion
is short-lived; a per-process cache is sufficient and avoids the
operational surface of a shared store.

Wall-clock TTL with monotonic-clock check (so a system clock jump
forward doesn't evict prematurely; clock jump backward leaves
entries slightly fresher than configured — acceptable). LRU eviction
when ``len(cache) > max_entries`` (least-recently-accessed entry
drops).
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict


def cache_key(provider: str, model: str, normalized_query: str) -> str:
    """SHA-256 of the join of (provider, model, normalized_query).

    Hashing — not concatenation — so a future PII / query-length
    audit on the cache contents doesn't need to redact raw queries.
    Hex-digest output keeps the key plain ASCII for any logging path
    that might surface it.
    """
    raw = f"{provider}\x00{model}\x00{normalized_query}".encode()
    return hashlib.sha256(raw).hexdigest()


class HydeCache:
    """Bounded TTL+LRU cache for HyDE pseudo-doc strings.

    Not thread-safe — the API is async-only and the worker runs each
    request on the event loop (no shared mutation across threads).
    If that ever changes, wrap operations with ``asyncio.Lock``.
    """

    def __init__(self, *, ttl_seconds: float = 600.0, max_entries: int = 256) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds!r}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries!r}")
        self._ttl = float(ttl_seconds)
        self._max_entries = int(max_entries)
        # OrderedDict gives us O(1) move-to-end for LRU touch +
        # O(1) popitem(last=False) for LRU eviction.
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def get(self, key: str) -> str | None:
        """Return the cached value for ``key``, or ``None`` on miss / expired.

        TTL-expired entries are evicted on access so the cache never
        serves stale data. LRU-touches the entry on hit.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self._ttl:
            # Stale — evict and miss.
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> None:
        """Insert (or overwrite) the entry, then enforce ``max_entries``.

        Re-inserting an existing key refreshes its timestamp + LRU position
        so a frequently-accessed query stays fresh.
        """
        if key in self._store:
            del self._store[key]
        self._store[key] = (time.monotonic(), value)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        """Drop every entry. Used by tests + the operator-driven cache
        purge if we ever expose one over the API."""
        self._store.clear()


__all__ = ["HydeCache", "cache_key"]
