"""#332 unit tests for :class:`HydeEngine` — orchestration semantics.

Three behaviors the spec pins explicitly:

* No generator → returns ``None`` silently.
* Generator raises → returns ``None`` + logs (never propagates).
* Cache hit → second call does NOT invoke generator.

Plus a few supporting cases for the empty-output path and the
classifier-gate-off path.
"""

from __future__ import annotations

import logging

import pytest

from cf_knowledge_kiln.generation import (
    GenerationResult,
    GeneratorProvider,
    MockGeneratorProvider,
)
from cf_knowledge_kiln.retrieval.hyde.cache import HydeCache
from cf_knowledge_kiln.retrieval.hyde.engine import HydeEngine


def _make_cache() -> HydeCache:
    return HydeCache(ttl_seconds=600, max_entries=32)


# ── classifier-gate path ──────────────────────────────────────────────


class TestClassifierGateOff:
    @pytest.mark.asyncio
    async def test_long_chatty_query_returns_none_without_generator_call(self) -> None:
        gen = MockGeneratorProvider(response_template="MOCK[{prompt}]")
        engine = HydeEngine(generator=gen, cache=_make_cache())
        # Long, low-jargon, non-imperative — gate should skip.
        result = await engine.expand(
            "we noticed yesterday that our team forgot to record the new "
            "decision in the operations log and now nobody can remember "
            "what was actually agreed upon"
        )
        assert result.pseudo_doc is None
        assert gen.calls == [], "gate-off path must not invoke the generator"


# ── no-generator path ────────────────────────────────────────────────


class TestNoGeneratorConfigured:
    @pytest.mark.asyncio
    async def test_returns_none_silently(self) -> None:
        engine = HydeEngine(generator=None, cache=_make_cache())
        # Short query → gate WOULD fire if a generator existed.
        result = await engine.expand("offsite backup failed")
        assert result.pseudo_doc is None

    @pytest.mark.asyncio
    async def test_no_warning_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The "no generator" path is the common dev-env shape; it
        should NOT log a warning every search. Pinned at debug-only."""
        engine = HydeEngine(generator=None, cache=_make_cache())
        with caplog.at_level(logging.WARNING):
            await engine.expand("offsite backup failed")
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warning_records, f"no-generator path must not log at WARNING+: {warning_records}"


# ── generator-error path ─────────────────────────────────────────────


class _RaisingGenerator:
    """A fake :class:`GeneratorProvider` that raises on every call."""

    provider: str = "raiser"
    model: str = "raiser-1"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.0,
        stop: list[str] | None = None,
    ) -> GenerationResult:
        self.calls += 1
        raise RuntimeError("synthetic generator failure")

    async def aclose(self) -> None:
        pass


class TestGeneratorRaises:
    @pytest.mark.asyncio
    async def test_returns_none(self) -> None:
        gen = _RaisingGenerator()
        engine = HydeEngine(generator=gen, cache=_make_cache())
        result = await engine.expand("offsite backup failed")
        assert result.pseudo_doc is None
        assert gen.calls == 1

    @pytest.mark.asyncio
    async def test_logs_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = HydeEngine(generator=_RaisingGenerator(), cache=_make_cache())
        with caplog.at_level(logging.ERROR):
            await engine.expand("offsite backup failed")
        msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("hyde" in m and "generator" in m for m in msgs), (
            f"generator failure must log at ERROR with hyde+generator markers: {msgs}"
        )

    @pytest.mark.asyncio
    async def test_failure_does_not_cache(self) -> None:
        """A raised generator MUST NOT persist anything to the cache,
        or the next call would also miss + retry (which is what we
        want) but we want to verify nothing leaked in."""
        cache = _make_cache()
        engine = HydeEngine(generator=_RaisingGenerator(), cache=cache)
        await engine.expand("offsite backup failed")
        assert len(cache) == 0


# ── empty-output path ────────────────────────────────────────────────


class _EmptyGenerator:
    provider: str = "empty"
    model: str = "empty-1"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.0,
        stop: list[str] | None = None,
    ) -> GenerationResult:
        self.calls += 1
        return GenerationResult(text="", finish_reason="content_filter", model=self.model)

    async def aclose(self) -> None:
        pass


class TestEmptyGeneratorOutput:
    @pytest.mark.asyncio
    async def test_returns_none(self) -> None:
        engine = HydeEngine(generator=_EmptyGenerator(), cache=_make_cache())
        result = await engine.expand("offsite backup failed")
        assert result.pseudo_doc is None

    @pytest.mark.asyncio
    async def test_does_not_cache(self) -> None:
        """Empty output is treated as transient (content-filter trip,
        upstream blip). Not cached so the next call retries."""
        cache = _make_cache()
        engine = HydeEngine(generator=_EmptyGenerator(), cache=cache)
        await engine.expand("offsite backup failed")
        assert len(cache) == 0


# ── happy-path + caching ─────────────────────────────────────────────


class TestSuccessfulExpansion:
    @pytest.mark.asyncio
    async def test_returns_generator_text(self) -> None:
        gen = MockGeneratorProvider(response_template="PSEUDO_DOC_FOR[{prompt}]")
        engine = HydeEngine(generator=gen, cache=_make_cache())
        result = await engine.expand("offsite backup failed")
        assert result.pseudo_doc is not None
        assert result.pseudo_doc.startswith("PSEUDO_DOC_FOR[")
        assert gen.calls and "offsite backup failed" in gen.calls[0]["prompt"]

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_second_generator_call(self) -> None:
        gen = MockGeneratorProvider(response_template="PSEUDO[{prompt}]")
        cache = _make_cache()
        engine = HydeEngine(generator=gen, cache=cache)
        first = await engine.expand("offsite backup failed")
        assert first.pseudo_doc is not None
        assert len(gen.calls) == 1
        second = await engine.expand("offsite backup failed")
        assert second.pseudo_doc == first.pseudo_doc
        assert len(gen.calls) == 1, "cache hit should NOT invoke the generator"

    @pytest.mark.asyncio
    async def test_normalization_makes_case_insensitive_cache_hit(self) -> None:
        """Queries that differ only in case + surrounding whitespace
        share a cache entry — same semantic query, same pseudo-doc."""
        gen = MockGeneratorProvider(response_template="PSEUDO[{prompt}]")
        cache = _make_cache()
        engine = HydeEngine(generator=gen, cache=cache)
        await engine.expand("Offsite Backup Failed")
        await engine.expand("offsite backup failed   ")
        # Both calls should hit the same cache key → only one
        # generator invocation.
        assert len(gen.calls) == 1

    @pytest.mark.asyncio
    async def test_different_queries_get_different_pseudo_docs(self) -> None:
        gen = MockGeneratorProvider(response_template="PSEUDO[{prompt}]")
        engine = HydeEngine(generator=gen, cache=_make_cache())
        a = await engine.expand("offsite backup failed")
        b = await engine.expand("how to rotate credhub CA")
        assert a.pseudo_doc is not None and b.pseudo_doc is not None
        assert a.pseudo_doc != b.pseudo_doc
        assert len(gen.calls) == 2


class TestProtocolCompliance:
    """Smoke check that ``HydeEngine`` accepts anything matching the
    :class:`GeneratorProvider` protocol — including the test-only
    fake classes used above."""

    def test_raising_generator_satisfies_protocol(self) -> None:
        gen: GeneratorProvider = _RaisingGenerator()  # type: ignore[assignment]
        assert gen.provider == "raiser"
        assert gen.model == "raiser-1"

    def test_empty_generator_satisfies_protocol(self) -> None:
        gen: GeneratorProvider = _EmptyGenerator()  # type: ignore[assignment]
        assert gen.provider == "empty"
        assert gen.model == "empty-1"
