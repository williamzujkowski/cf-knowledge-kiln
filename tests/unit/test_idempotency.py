"""Unit tests for the idempotency dispatch module (#309)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cf_knowledge_kiln.api.idempotency import (
    HEADER,
    REPLAY_HEADER,
    Outcome,
    canonical_body_hash,
    check_or_replay,
    extract_key,
    should_cache,
)


class TestHeaderName:
    def test_request_header_name(self) -> None:
        """Industry standard per draft-ietf-httpapi-idempotency-key."""
        assert HEADER == "Idempotency-Key"

    def test_replay_response_header_name(self) -> None:
        """Server adds this on a replay so the agent can detect
        cached vs fresh responses."""
        assert REPLAY_HEADER == "Idempotency-Replayed"


class TestCanonicalBodyHash:
    """Two bodies that ARE semantically the same MUST hash equal;
    bodies that differ MUST hash differently."""

    def test_empty_body_hashes_stably(self) -> None:
        assert canonical_body_hash(None) == canonical_body_hash({})

    def test_key_order_independent(self) -> None:
        """An agent that re-serializes via a non-sorting library
        must not get false conflicts."""
        a = {"a": 1, "b": 2, "c": 3}
        b = {"c": 3, "b": 2, "a": 1}
        assert canonical_body_hash(a) == canonical_body_hash(b)

    def test_nested_key_order_independent(self) -> None:
        a = {"outer": {"a": 1, "b": 2}, "z": 9}
        b = {"z": 9, "outer": {"b": 2, "a": 1}}
        assert canonical_body_hash(a) == canonical_body_hash(b)

    def test_value_change_changes_hash(self) -> None:
        assert canonical_body_hash({"max_tokens": 3000}) != canonical_body_hash(
            {"max_tokens": 4000}
        )

    def test_hash_is_64_hex_chars(self) -> None:
        result = canonical_body_hash({"x": 1})
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


class TestExtractKey:
    def _req(self, value):
        class _H:
            def __init__(self, v):
                self._v = v

            def get(self, name):
                return self._v if name == HEADER else None

        class _R:
            def __init__(self, v):
                self.headers = _H(v)

        return _R(value)

    def test_no_header_returns_none(self) -> None:
        assert extract_key(self._req(None)) is None

    def test_empty_header_returns_none(self) -> None:
        assert extract_key(self._req("")) is None
        assert extract_key(self._req("   ")) is None

    def test_valid_uuid_passes_through(self) -> None:
        assert extract_key(self._req("4b3f1c8e-0d2a-4f3e")) == "4b3f1c8e-0d2a-4f3e"

    def test_dangerous_chars_scrubbed(self) -> None:
        """Newline / quote / slash injection scrubbed."""
        assert extract_key(self._req("foo\nbar/baz")) == "foo_bar_baz"


class TestShouldCache:
    """2xx + 4xx yes, 5xx no (transient — caching breaks retry contract)."""

    @pytest.mark.parametrize("status", [200, 201, 204, 299])
    def test_2xx_cached(self, status: int) -> None:
        assert should_cache(status) is True

    @pytest.mark.parametrize("status", [400, 401, 404, 422, 429, 499])
    def test_4xx_cached(self, status: int) -> None:
        assert should_cache(status) is True

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_not_cached(self, status: int) -> None:
        assert should_cache(status) is False

    @pytest.mark.parametrize("status", [100, 199])
    def test_1xx_not_cached(self, status: int) -> None:
        assert should_cache(status) is False


class TestOutcomeEnum:
    def test_miss_string_value(self) -> None:
        assert Outcome.MISS == "miss"

    def test_hit_string_value(self) -> None:
        assert Outcome.HIT == "hit"

    def test_conflict_string_value(self) -> None:
        assert Outcome.CONFLICT == "conflict"

    def test_outcomes_are_distinct(self) -> None:
        assert len({Outcome.MISS, Outcome.HIT, Outcome.CONFLICT}) == 3


# ─── Expiry semantics (review finding #1, BLOCKER) ──────────────


@dataclass
class _StubCached:
    """Stand-in for the ORM row returned by IdempotencyRepository.lookup.

    Only the fields check_or_replay touches matter for these tests.
    """

    request_hash: str
    response_body: dict[str, Any]
    response_status: int
    expires_at: datetime


class _StubRepo:
    """Replaces IdempotencyRepository so check_or_replay can be exercised
    without standing up a real DB. We only need lookup; create/store
    live on a different code path."""

    def __init__(self, row: _StubCached | None) -> None:
        self.row = row
        self.calls: list[tuple[str, str]] = []

    async def lookup(self, *, key: str, route: str) -> _StubCached | None:
        self.calls.append((key, route))
        return self.row


def _stub_request(headers: dict[str, str] | None = None) -> Any:
    """Minimal Request stand-in: only .headers.get is touched here."""

    class _H:
        def __init__(self, h: dict[str, str]) -> None:
            self._h = h

        def get(self, name: str) -> str | None:
            return self._h.get(name)

    class _R:
        def __init__(self, h: dict[str, str]) -> None:
            self.headers = _H(h)

    return _R(headers or {})


class TestCheckOrReplayExpiry:
    """An expired cached row must NOT replay — the documented 24h TTL is
    the public contract and silently extending it is the bug-class the
    blind review caught."""

    @pytest.mark.asyncio
    async def test_expired_row_returns_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {"q": "x"}
        row = _StubCached(
            request_hash=canonical_body_hash(body),
            response_body={"echo": "x"},
            response_status=200,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),  # one second past
        )
        stub = _StubRepo(row)
        monkeypatch.setattr(
            "cf_knowledge_kiln.api.idempotency.IdempotencyRepository",
            lambda _session: stub,
        )
        result = await check_or_replay(
            session=None,  # never touched; the repo is stubbed
            request=_stub_request({HEADER: "key-1234567890"}),
            route="/v1/search",
            body=body,
        )
        # Expired → MISS so the handler re-runs; not HIT.
        assert result.outcome == Outcome.MISS

    @pytest.mark.asyncio
    async def test_fresh_row_still_hits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {"q": "x"}
        row = _StubCached(
            request_hash=canonical_body_hash(body),
            response_body={"echo": "x"},
            response_status=200,
            expires_at=datetime.now(UTC) + timedelta(hours=23),
        )
        stub = _StubRepo(row)
        monkeypatch.setattr(
            "cf_knowledge_kiln.api.idempotency.IdempotencyRepository",
            lambda _session: stub,
        )
        result = await check_or_replay(
            session=None,
            request=_stub_request({HEADER: "key-1234567890"}),
            route="/v1/search",
            body=body,
        )
        assert result.outcome == Outcome.HIT
        assert result.cached_body == {"echo": "x"}
        assert result.cached_status == 200

    @pytest.mark.asyncio
    async def test_expired_row_with_body_mismatch_still_returns_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Expired row + different body still misses — expiry wins. We
        don't want to surface ``idempotency_conflict`` for a row that's
        about to be reaped anyway; the user's retry is logically with a
        fresh key."""
        row = _StubCached(
            request_hash=canonical_body_hash({"q": "old"}),
            response_body={"echo": "old"},
            response_status=200,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        stub = _StubRepo(row)
        monkeypatch.setattr(
            "cf_knowledge_kiln.api.idempotency.IdempotencyRepository",
            lambda _session: stub,
        )
        result = await check_or_replay(
            session=None,
            request=_stub_request({HEADER: "key-1234567890"}),
            route="/v1/search",
            body={"q": "new"},  # different body
        )
        assert result.outcome == Outcome.MISS
