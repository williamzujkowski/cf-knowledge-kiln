"""Unit tests for the in-process token-bucket rate limiter (#79)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException, Request

from cf_knowledge_kiln.api.rate_limit import (
    TokenBucketLimiter,
    client_ip,
    raise_429_if_limited,
)


class TestTokenBucketLimiter:
    def test_rejects_invalid_capacity(self) -> None:
        with pytest.raises(ValueError):
            TokenBucketLimiter(capacity=0, window_seconds=60.0)

    def test_rejects_invalid_window(self) -> None:
        with pytest.raises(ValueError):
            TokenBucketLimiter(capacity=10, window_seconds=0)

    def test_allows_up_to_capacity_then_denies(self) -> None:
        limiter = TokenBucketLimiter(capacity=3, window_seconds=60.0)
        # First 3 hits at t=0 succeed, the 4th is denied.
        assert limiter.hit("1.2.3.4", now=0.0) is True
        assert limiter.hit("1.2.3.4", now=0.0) is True
        assert limiter.hit("1.2.3.4", now=0.0) is True
        assert limiter.hit("1.2.3.4", now=0.0) is False

    def test_separate_keys_have_separate_buckets(self) -> None:
        limiter = TokenBucketLimiter(capacity=1, window_seconds=60.0)
        assert limiter.hit("a", now=0.0) is True
        assert limiter.hit("b", now=0.0) is True
        # Both buckets are now empty.
        assert limiter.hit("a", now=0.0) is False
        assert limiter.hit("b", now=0.0) is False

    def test_refills_at_configured_rate(self) -> None:
        # capacity=60 per 60 s → 1 token per second.
        limiter = TokenBucketLimiter(capacity=60, window_seconds=60.0)
        for _ in range(60):
            assert limiter.hit("k", now=0.0) is True
        assert limiter.hit("k", now=0.0) is False
        # 1 second later → 1 token refilled.
        assert limiter.hit("k", now=1.0) is True
        assert limiter.hit("k", now=1.0) is False

    def test_refill_caps_at_capacity(self) -> None:
        limiter = TokenBucketLimiter(capacity=5, window_seconds=10.0)
        # Drain.
        for _ in range(5):
            assert limiter.hit("k", now=0.0) is True
        # Sleep way past the window — should refill to capacity, not above.
        for _ in range(5):
            assert limiter.hit("k", now=1_000.0) is True
        assert limiter.hit("k", now=1_000.0) is False

    def test_retry_after_for_unknown_key(self) -> None:
        limiter = TokenBucketLimiter(capacity=1, window_seconds=60.0)
        assert limiter.retry_after("never-seen", now=0.0) == 1

    def test_retry_after_when_tokens_available(self) -> None:
        limiter = TokenBucketLimiter(capacity=2, window_seconds=60.0)
        limiter.hit("k", now=0.0)  # 1 token left
        assert limiter.retry_after("k", now=0.0) == 1

    def test_retry_after_when_drained(self) -> None:
        # capacity=60 per 60 s → 1 token/sec → retry ≥1 second.
        limiter = TokenBucketLimiter(capacity=60, window_seconds=60.0)
        for _ in range(60):
            limiter.hit("k", now=0.0)
        retry = limiter.retry_after("k", now=0.0)
        assert retry >= 1

    def test_clock_jump_backward_does_not_negative_refill(self) -> None:
        """A non-monotonic ``now`` must not subtract tokens."""
        limiter = TokenBucketLimiter(capacity=2, window_seconds=60.0)
        assert limiter.hit("k", now=10.0) is True
        # Time jumps backward — elapsed must clamp to 0, not go negative.
        assert limiter.hit("k", now=5.0) is True
        # The first call drained 1 token. The second (backward jump) drained
        # the other. Now we're at zero — third call should deny.
        assert limiter.hit("k", now=5.0) is False
        # retry_after under a backward jump must still return ≥1.
        assert limiter.retry_after("k", now=4.0) >= 1

    def test_evicts_oldest_when_max_buckets_exceeded(self) -> None:
        """LRU eviction keeps the bucket dict from growing without bound."""
        limiter = TokenBucketLimiter(capacity=1, window_seconds=60.0, max_buckets=3)
        # Fill to cap, drain each.
        for k in ("a", "b", "c"):
            assert limiter.hit(k, now=0.0) is True
            assert limiter.hit(k, now=0.0) is False
        # Hit a new key — should evict "a" (oldest).
        assert limiter.hit("d", now=0.0) is True
        # "a" is gone — fresh bucket means a clean allow.
        assert limiter.hit("a", now=0.0) is True

    def test_recent_keys_promoted_against_eviction(self) -> None:
        """Accessing an existing key keeps it from being evicted."""
        limiter = TokenBucketLimiter(capacity=3, window_seconds=60.0, max_buckets=3)
        # Seed three buckets — each holds 2 tokens after one hit.
        limiter.hit("a", now=0.0)
        limiter.hit("b", now=0.0)
        limiter.hit("c", now=0.0)
        # Touch "a" — makes "a" the most-recently used; "b" is now oldest.
        limiter.hit("a", now=0.0)
        # Fourth distinct key triggers eviction of "b" (the oldest).
        limiter.hit("d", now=0.0)
        # "a" still has its existing bucket — drained 2 of 3, so 1 token left.
        assert limiter.hit("a", now=0.0) is True
        assert limiter.hit("a", now=0.0) is False
        # "b" was evicted — fresh bucket with capacity 3.
        for _ in range(3):
            assert limiter.hit("b", now=0.0) is True
        assert limiter.hit("b", now=0.0) is False

    def test_concurrent_hits_respect_capacity(self) -> None:
        """Lock prevents over-spend under thread contention."""
        limiter = TokenBucketLimiter(capacity=100, window_seconds=60.0)

        # Fire 200 hits across 8 workers — at the same monotonic moment,
        # so no refill happens between them. Exactly 100 should allow.
        def fire() -> bool:
            return limiter.hit("shared", now=0.0)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: fire(), range(200)))
        allows = sum(1 for r in results if r)
        denies = sum(1 for r in results if not r)
        assert allows == 100
        assert denies == 100

    def test_max_buckets_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            TokenBucketLimiter(capacity=1, window_seconds=60.0, max_buckets=0)


class TestClientIp:
    def _make_request(
        self,
        *,
        peer: str | None = "10.0.0.1",
        xff: str | None = None,
    ) -> Request:
        headers: list[tuple[bytes, bytes]] = []
        if xff is not None:
            headers.append((b"x-forwarded-for", xff.encode("latin-1")))
        scope: dict[str, object] = {
            "type": "http",
            "method": "POST",
            "path": "/v1/search",
            "headers": headers,
            "client": (peer, 12345) if peer else None,
        }
        return Request(scope)  # type: ignore[arg-type]

    def test_no_xff_returns_peer(self) -> None:
        req = self._make_request(peer="10.0.0.1")
        assert client_ip(req) == "10.0.0.1"

    def test_xff_ignored_when_trust_xff_false(self) -> None:
        """Default: never trust client-supplied XFF — local-dev safe."""
        req = self._make_request(peer="10.0.0.1", xff="198.51.100.7")
        assert client_ip(req) == "10.0.0.1"
        assert client_ip(req, trust_xff=False) == "10.0.0.1"

    def test_single_xff_returns_it_when_trusted(self) -> None:
        req = self._make_request(peer="10.0.0.1", xff="198.51.100.7")
        assert client_ip(req, trust_xff=True) == "198.51.100.7"

    def test_multi_hop_xff_returns_first_when_trusted(self) -> None:
        req = self._make_request(
            peer="10.0.0.1",
            xff="198.51.100.7, 10.0.0.2, 10.0.0.3",
        )
        assert client_ip(req, trust_xff=True) == "198.51.100.7"

    def test_ipv6_brackets_normalized(self) -> None:
        """``[::1]`` and ``::1`` must map to the same key."""
        req_brackets = self._make_request(peer="10.0.0.1", xff="[::1]")
        req_plain = self._make_request(peer="10.0.0.1", xff="::1")
        assert client_ip(req_brackets, trust_xff=True) == client_ip(req_plain, trust_xff=True)

    def test_ipv6_brackets_with_port_normalized(self) -> None:
        """``[::1]:1234`` collapses to ``::1`` (port stripped, case-folded)."""
        req = self._make_request(peer=None, xff="[::1]:1234")
        assert client_ip(req, trust_xff=True) == "::1"

    def test_case_folded(self) -> None:
        """IPv6 hex case is normalized so ``::FFFF`` == ``::ffff``."""
        upper = self._make_request(peer=None, xff="2001:DB8::1")
        lower = self._make_request(peer=None, xff="2001:db8::1")
        assert client_ip(upper, trust_xff=True) == client_ip(lower, trust_xff=True)

    def test_missing_client_falls_back_to_unknown(self) -> None:
        req = self._make_request(peer=None, xff=None)
        assert client_ip(req) == "unknown"


class TestRaise429:
    def _make_request(self) -> Request:
        scope: dict[str, object] = {
            "type": "http",
            "method": "POST",
            "path": "/v1/search",
            "headers": [],
            "client": ("10.0.0.1", 12345),
        }
        return Request(scope)  # type: ignore[arg-type]

    def test_allows_when_under_limit(self) -> None:
        limiter = TokenBucketLimiter(capacity=2, window_seconds=60.0)
        req = self._make_request()
        # First two calls do not raise.
        raise_429_if_limited(limiter, req)
        raise_429_if_limited(limiter, req)

    def test_raises_429_with_retry_after_header(self) -> None:
        limiter = TokenBucketLimiter(capacity=1, window_seconds=60.0)
        req = self._make_request()
        raise_429_if_limited(limiter, req)
        with pytest.raises(HTTPException) as exc:
            raise_429_if_limited(limiter, req)
        assert exc.value.status_code == 429
        assert exc.value.headers is not None
        assert "Retry-After" in exc.value.headers
        # Header value is a stringified positive int.
        assert int(exc.value.headers["Retry-After"]) >= 1
