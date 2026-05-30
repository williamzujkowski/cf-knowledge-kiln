"""#332 unit tests for :class:`HydeCache` — TTL + LRU semantics.

Uses :func:`time.monotonic` patching via ``monkeypatch`` so the TTL
tests don't depend on wall-clock progression.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.retrieval.hyde.cache import HydeCache, cache_key


class TestCacheKey:
    """Key derivation. Verifies the contract that swapping ANY of
    (provider, model, normalized_query) invalidates."""

    def test_key_is_deterministic(self) -> None:
        a = cache_key("openai", "gpt-4o-mini", "offsite backup failed")
        b = cache_key("openai", "gpt-4o-mini", "offsite backup failed")
        assert a == b

    def test_key_differs_by_provider(self) -> None:
        a = cache_key("openai", "gpt-4o-mini", "q")
        b = cache_key("anthropic", "gpt-4o-mini", "q")
        assert a != b

    def test_key_differs_by_model(self) -> None:
        a = cache_key("openai", "gpt-4o-mini", "q")
        b = cache_key("openai", "gpt-4", "q")
        assert a != b

    def test_key_differs_by_query(self) -> None:
        a = cache_key("openai", "gpt-4o-mini", "offsite backup failed")
        b = cache_key("openai", "gpt-4o-mini", "offsite backup")
        assert a != b

    def test_key_is_hex_sha256(self) -> None:
        key = cache_key("p", "m", "q")
        assert len(key) == 64
        int(key, 16)  # no exception → valid hex


class TestHydeCacheConstruction:
    def test_rejects_non_positive_ttl(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            HydeCache(ttl_seconds=0)
        with pytest.raises(ValueError, match="ttl_seconds"):
            HydeCache(ttl_seconds=-1)

    def test_rejects_non_positive_max_entries(self) -> None:
        with pytest.raises(ValueError, match="max_entries"):
            HydeCache(max_entries=0)
        with pytest.raises(ValueError, match="max_entries"):
            HydeCache(max_entries=-5)


class TestGetPut:
    def test_miss_returns_none(self) -> None:
        c = HydeCache(ttl_seconds=60, max_entries=10)
        assert c.get("absent") is None

    def test_put_then_get_returns_value(self) -> None:
        c = HydeCache(ttl_seconds=60, max_entries=10)
        c.put("k", "v")
        assert c.get("k") == "v"

    def test_put_overwrites(self) -> None:
        c = HydeCache(ttl_seconds=60, max_entries=10)
        c.put("k", "first")
        c.put("k", "second")
        assert c.get("k") == "second"

    def test_len_tracks_entries(self) -> None:
        c = HydeCache(ttl_seconds=60, max_entries=10)
        assert len(c) == 0
        c.put("a", "1")
        c.put("b", "2")
        assert len(c) == 2

    def test_clear_drops_everything(self) -> None:
        c = HydeCache(ttl_seconds=60, max_entries=10)
        c.put("a", "1")
        c.clear()
        assert c.get("a") is None
        assert len(c) == 0


class TestTTLExpiry:
    """Patch ``time.monotonic`` to advance the clock without
    sleeping. The TTL check inside the cache calls ``time.monotonic``
    so this works regardless of whether the rest of the process
    moves real time."""

    def test_entry_serves_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = {"v": 1000.0}
        monkeypatch.setattr(
            "cf_knowledge_kiln.retrieval.hyde.cache.time.monotonic", lambda: now["v"]
        )
        c = HydeCache(ttl_seconds=60, max_entries=10)
        c.put("k", "v")
        now["v"] = 1059.0  # 59s later — still within TTL
        assert c.get("k") == "v"

    def test_entry_expired_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = {"v": 1000.0}
        monkeypatch.setattr(
            "cf_knowledge_kiln.retrieval.hyde.cache.time.monotonic", lambda: now["v"]
        )
        c = HydeCache(ttl_seconds=60, max_entries=10)
        c.put("k", "v")
        now["v"] = 1061.0  # 61s later — past TTL
        assert c.get("k") is None

    def test_expired_entry_is_evicted_on_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Accessing an expired entry drops it — the cache never holds
        onto stale rows past their TTL, even when the eviction LRU
        would otherwise keep them."""
        now = {"v": 1000.0}
        monkeypatch.setattr(
            "cf_knowledge_kiln.retrieval.hyde.cache.time.monotonic", lambda: now["v"]
        )
        c = HydeCache(ttl_seconds=60, max_entries=10)
        c.put("k", "v")
        assert len(c) == 1
        now["v"] = 1100.0  # past TTL
        c.get("k")  # triggers eviction
        assert len(c) == 0


class TestLRUEviction:
    def test_oldest_unused_evicted_first(self) -> None:
        c = HydeCache(ttl_seconds=600, max_entries=3)
        c.put("a", "1")
        c.put("b", "2")
        c.put("c", "3")
        # Touch 'a' so it becomes most-recently-used.
        c.get("a")
        c.put("d", "4")  # eviction triggers → 'b' is oldest unused
        assert c.get("a") == "1"
        assert c.get("b") is None
        assert c.get("c") == "3"
        assert c.get("d") == "4"

    def test_put_evicts_below_max(self) -> None:
        c = HydeCache(ttl_seconds=600, max_entries=2)
        c.put("a", "1")
        c.put("b", "2")
        c.put("c", "3")
        assert len(c) == 2
        assert c.get("a") is None

    def test_overwrite_does_not_count_as_new_entry(self) -> None:
        """Re-putting an existing key keeps total entries the same
        and refreshes the value."""
        c = HydeCache(ttl_seconds=600, max_entries=2)
        c.put("a", "1")
        c.put("b", "2")
        c.put("a", "1-updated")  # overwrite; no eviction (still 2 entries)
        assert len(c) == 2
        assert c.get("a") == "1-updated"
        assert c.get("b") == "2"

    def test_overwrite_refreshes_lru_position(self) -> None:
        """Re-putting an existing key bumps it to MRU; the previously-
        MRU entry that wasn't overwritten becomes LRU and is the next
        eviction target."""
        c = HydeCache(ttl_seconds=600, max_entries=2)
        c.put("a", "1")
        c.put("b", "2")
        # After: ['a' (LRU), 'b' (MRU)]
        c.put("a", "1-updated")
        # After: ['b' (LRU), 'a' (MRU)] — 'a' was re-inserted, so it's MRU now.
        c.put("c", "3")  # eviction: 'b' is LRU, dropped.
        assert c.get("a") == "1-updated"
        assert c.get("b") is None
        assert c.get("c") == "3"
