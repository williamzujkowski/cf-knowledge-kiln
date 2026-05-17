"""Unit tests for the in-process token-bucket rate limiter (#79)."""

from __future__ import annotations

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

    def test_single_xff_returns_it(self) -> None:
        req = self._make_request(peer="10.0.0.1", xff="198.51.100.7")
        assert client_ip(req) == "198.51.100.7"

    def test_multi_hop_xff_returns_first(self) -> None:
        req = self._make_request(
            peer="10.0.0.1",
            xff="198.51.100.7, 10.0.0.2, 10.0.0.3",
        )
        assert client_ip(req) == "198.51.100.7"

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
