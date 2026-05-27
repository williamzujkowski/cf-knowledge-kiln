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

    def test_oversized_input_truncated_to_max_key_len(self) -> None:
        """Sanitizer truncates at MAX_KEY_LEN; extract_key honors it.

        Belt-and-braces (#320 follow-up): even if a future refactor
        of the sanitizer ever produced a longer string, extract_key
        re-clamps at the post-sanitize step. Pin the invariant from
        the caller side."""
        from cf_knowledge_kiln.api.idempotency import MAX_KEY_LEN

        # Build a value that's clearly too long (10x the cap) using
        # only the allowed charset so the sanitizer's char-scrub
        # doesn't accidentally shrink it via replacement.
        oversized = "a" * (MAX_KEY_LEN * 10)
        key = extract_key(self._req(oversized))
        assert key is not None
        assert len(key) == MAX_KEY_LEN

    def test_key_under_max_returns_unchanged(self) -> None:
        """A legal key well under the cap passes through verbatim."""
        from cf_knowledge_kiln.api.idempotency import MAX_KEY_LEN

        # 64-char hex is the common UUID-without-dashes shape.
        value = "a" * 64
        assert len(value) < MAX_KEY_LEN
        assert extract_key(self._req(value)) == value


class TestMaxKeyLenExported:
    """MAX_KEY_LEN is the wire invariant other modules + tests grep for.
    Pin its value here so a silent bump doesn't slip through review."""

    def test_max_key_len_is_200(self) -> None:
        from cf_knowledge_kiln.api.idempotency import MAX_KEY_LEN

        assert MAX_KEY_LEN == 200


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


# ─── #320 items 1+2: race-safe store + exception discrimination ──


class _RecordingLogger:
    """Capture log calls so we can assert WARNING vs ERROR vs INFO."""

    def __init__(self) -> None:
        self.info_msgs: list[str] = []
        self.warning_msgs: list[str] = []
        self.error_msgs: list[str] = []
        self.exception_msgs: list[str] = []

    def info(self, msg: str, *args: object, **_kw: object) -> None:
        self.info_msgs.append(msg % args if args else msg)

    def warning(self, msg: str, *args: object, **_kw: object) -> None:
        self.warning_msgs.append(msg % args if args else msg)

    def error(self, msg: str, *args: object, **_kw: object) -> None:
        self.error_msgs.append(msg % args if args else msg)

    def exception(self, msg: str, *args: object, **_kw: object) -> None:
        self.exception_msgs.append(msg % args if args else msg)


class _StubAsyncCtx:
    """Minimal async-with stub for session.begin_nested()."""

    async def __aenter__(self) -> _StubAsyncCtx:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _StubSession:
    """Minimal AsyncSession stand-in: only begin_nested is used by store()."""

    def begin_nested(self) -> _StubAsyncCtx:
        return _StubAsyncCtx()


class _StubRepoForStore:
    """Replaces IdempotencyRepository so store() can be exercised
    without standing up Postgres."""

    def __init__(self, *, returns: bool = True, raises: Exception | None = None) -> None:
        self.returns = returns
        self.raises = raises
        self.create_if_absent_called = False

    async def create_if_absent(self, **_kwargs: object) -> bool:
        self.create_if_absent_called = True
        if self.raises is not None:
            raise self.raises
        return self.returns


class TestStoreRaceSafety:
    """#320 item 1: concurrent same-key submissions can't race-error.

    The dispatcher calls create_if_absent (INSERT … ON CONFLICT DO NOTHING),
    which returns False when a sibling request already cached the row.
    store() must log INFO (not WARNING/ERROR) and return cleanly.
    """

    @pytest.mark.asyncio
    async def test_race_lost_returns_cleanly_with_info_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cf_knowledge_kiln.api import idempotency as mod

        repo = _StubRepoForStore(returns=False)  # race-lost shape
        rec = _RecordingLogger()
        monkeypatch.setattr(mod, "IdempotencyRepository", lambda _s: repo)
        monkeypatch.setattr(mod, "logger", rec)

        await mod.store(
            session=_StubSession(),
            key="dash-shaped-value-1",
            route="/v1/search",
            request_hash="abc",
            resource_id=None,
            response_body={"x": 1},
            response_status=200,
        )

        assert repo.create_if_absent_called is True
        assert rec.warning_msgs == []  # NOT a warning — race is expected
        assert rec.error_msgs == []
        assert rec.exception_msgs == []
        # INFO log carries enough context for an operator to grep
        assert len(rec.info_msgs) == 1
        assert "race observed" in rec.info_msgs[0]

    @pytest.mark.asyncio
    async def test_fresh_insert_logs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cf_knowledge_kiln.api import idempotency as mod

        repo = _StubRepoForStore(returns=True)  # fresh insert
        rec = _RecordingLogger()
        monkeypatch.setattr(mod, "IdempotencyRepository", lambda _s: repo)
        monkeypatch.setattr(mod, "logger", rec)

        await mod.store(
            session=_StubSession(),
            key="dash-shaped-value-2",
            route="/v1/search",
            request_hash="abc",
            resource_id=None,
            response_body={"x": 1},
            response_status=200,
        )

        # Happy path: nothing logged at any level.
        assert rec.info_msgs == []
        assert rec.warning_msgs == []
        assert rec.error_msgs == []
        assert rec.exception_msgs == []


class TestStoreExceptionDiscrimination:
    """#320 item 2: distinguish OperationalError (transient) from
    IntegrityError (corruption) from the generic catch-all."""

    @pytest.mark.asyncio
    async def test_operational_error_logs_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sqlalchemy.exc import OperationalError

        from cf_knowledge_kiln.api import idempotency as mod

        # OperationalError signature: (statement, params, orig)
        exc = OperationalError("SELECT 1", {}, Exception("connection dropped"))
        repo = _StubRepoForStore(raises=exc)
        rec = _RecordingLogger()
        monkeypatch.setattr(mod, "IdempotencyRepository", lambda _s: repo)
        monkeypatch.setattr(mod, "logger", rec)

        await mod.store(
            session=_StubSession(),
            key="dash-shaped-value-3",
            route="/v1/search",
            request_hash="abc",
            resource_id=None,
            response_body={"x": 1},
            response_status=200,
        )

        # Transient → WARNING (operator-noticeable but not actionable);
        # NOT exception() (which carries traceback and reads as bug).
        assert len(rec.warning_msgs) == 1
        assert "transient DB error" in rec.warning_msgs[0]
        assert rec.error_msgs == []
        assert rec.exception_msgs == []

    @pytest.mark.asyncio
    async def test_integrity_error_logs_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sqlalchemy.exc import IntegrityError

        from cf_knowledge_kiln.api import idempotency as mod

        # IntegrityError surfaces when ON CONFLICT didn't deflect —
        # i.e. genuine schema corruption (NOT the (key, route) race).
        exc = IntegrityError("INSERT", {}, Exception("not-null violation on column foo"))
        repo = _StubRepoForStore(raises=exc)
        rec = _RecordingLogger()
        monkeypatch.setattr(mod, "IdempotencyRepository", lambda _s: repo)
        monkeypatch.setattr(mod, "logger", rec)

        await mod.store(
            session=_StubSession(),
            key="dash-shaped-value-4",
            route="/v1/search",
            request_hash="abc",
            resource_id=None,
            response_body={"x": 1},
            response_status=200,
        )

        # Real corruption → exception() with traceback so operator
        # has the stack on hand.
        assert len(rec.exception_msgs) == 1
        assert "integrity constraint" in rec.exception_msgs[0]
        assert rec.warning_msgs == []

    @pytest.mark.asyncio
    async def test_unknown_exception_logs_exception_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cf_knowledge_kiln.api import idempotency as mod

        repo = _StubRepoForStore(raises=RuntimeError("disk full"))
        rec = _RecordingLogger()
        monkeypatch.setattr(mod, "IdempotencyRepository", lambda _s: repo)
        monkeypatch.setattr(mod, "logger", rec)

        # Crucially: must NOT re-raise. The handler response stands.
        await mod.store(
            session=_StubSession(),
            key="dash-shaped-value-5",
            route="/v1/search",
            request_hash="abc",
            resource_id=None,
            response_body={"x": 1},
            response_status=200,
        )

        assert len(rec.exception_msgs) == 1
        assert "non-fatal" in rec.exception_msgs[0]
