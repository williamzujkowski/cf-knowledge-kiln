"""#404 — HyDE span metadata pins.

The pre-PR ``HydeEngine.expand`` returned ``str | None``; the
``retrieval.hyde`` OTel span carried only ``gated_on``. The audit
needed two more attributes — ``cache_hit`` and ``generation_ms`` —
that the legacy return shape couldn't express without out-of-band
state on the engine instance.

This PR refactors the return type to :class:`HydeResult` (frozen
dataclass) carrying:

* ``pseudo_doc: str | None`` — the expanded text; ``None`` means
  "skip HyDE" with the same semantics as before.
* ``cache_hit: bool`` — true ONLY when the cache short-circuited the
  generator call. Skipped paths (classifier-off, no-generator,
  generator-raised, empty-output) all report ``False``.
* ``generation_ms: float | None`` — wall-clock duration of the
  generator call when there was a cache miss. ``None`` for any path
  that did not actually call the generator.

These two are the load-bearing knobs operators use to tune HyDE in
production: cache_hit reports whether the cache is doing its job;
generation_ms reports per-call latency for the upstream LLM.
"""

from __future__ import annotations

import asyncio

import pytest

from cf_knowledge_kiln.generation import (
    GenerationResult,
    GeneratorProvider,
    MockGeneratorProvider,
)
from cf_knowledge_kiln.retrieval.hyde import HydeEngine, HydeResult
from cf_knowledge_kiln.retrieval.hyde.cache import HydeCache


def _make_cache() -> HydeCache:
    return HydeCache(ttl_seconds=600, max_entries=32)


# ── shape ────────────────────────────────────────────────────────────


def test_hyde_result_is_frozen_dataclass() -> None:
    """Pin immutability so a future caller can't mutate the span data
    after the engine returns. ``cache_hit`` and ``generation_ms`` are
    span attributes; a mutable result would let downstream code
    silently lie to telemetry."""
    result = HydeResult(pseudo_doc="hi", cache_hit=True, generation_ms=12.5)
    with pytest.raises(AttributeError):
        result.cache_hit = False  # type: ignore[misc]


def test_hyde_result_defaults_are_skip_safe() -> None:
    """Default field values match the "skip HyDE" path: no doc, no
    cache hit, no generation. Constructing a bare ``HydeResult()``
    should be valid and represent "the engine declined.\""""
    result = HydeResult(pseudo_doc=None)
    assert result.pseudo_doc is None
    assert result.cache_hit is False
    assert result.generation_ms is None


# ── classifier-off path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_off_returns_no_metadata() -> None:
    """When the classifier gate skips HyDE, no generation happened
    and no cache was touched — both metadata fields stay at the
    skip-safe defaults so the span carries truthful zeros."""
    gen = MockGeneratorProvider(response_template="X")
    engine = HydeEngine(generator=gen, cache=_make_cache())
    result = await engine.expand(
        "we noticed yesterday that our team forgot to record the new "
        "decision in the operations log and now nobody can remember "
        "what was actually agreed upon"
    )
    assert isinstance(result, HydeResult)
    assert result.pseudo_doc is None
    assert result.cache_hit is False
    assert result.generation_ms is None
    assert gen.calls == []


# ── happy-path: cache miss ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_miss_reports_generation_ms_and_false_cache_hit() -> None:
    """First call against an empty cache must:
    - return a non-None pseudo_doc
    - report cache_hit == False
    - report a positive generation_ms (the generator was actually
      called, even if the mock is instant — we measure SOMETHING)."""
    gen = MockGeneratorProvider(response_template="PSEUDO[{prompt}]")
    engine = HydeEngine(generator=gen, cache=_make_cache())
    result = await engine.expand("offsite backup failed")
    assert isinstance(result, HydeResult)
    assert result.pseudo_doc is not None
    assert result.cache_hit is False
    assert result.generation_ms is not None
    assert result.generation_ms >= 0.0, (
        f"generation_ms must be a non-negative float; got {result.generation_ms!r}"
    )


# ── happy-path: cache hit ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_reports_true_and_no_generation_ms() -> None:
    """Second call against the warmed cache must:
    - return the same pseudo_doc as the first call
    - report cache_hit == True
    - report generation_ms == None (no generator call happened)."""
    gen = MockGeneratorProvider(response_template="PSEUDO[{prompt}]")
    cache = _make_cache()
    engine = HydeEngine(generator=gen, cache=cache)
    first = await engine.expand("offsite backup failed")
    second = await engine.expand("offsite backup failed")
    assert first.pseudo_doc == second.pseudo_doc
    assert second.cache_hit is True
    assert second.generation_ms is None
    assert len(gen.calls) == 1, "cache hit should not invoke the generator a second time"


# ── error paths ──────────────────────────────────────────────────────


class _RaisingGenerator:
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


@pytest.mark.asyncio
async def test_generator_raises_reports_skip_safe_metadata() -> None:
    """When the generator raises, the engine swallows + returns
    skip-safe defaults. cache_hit must be False (we tried the
    generator, not the cache); generation_ms must be None (the call
    didn't complete; reporting a partial timing would mislead
    operators about how long their LLM blocks before failing)."""
    gen: GeneratorProvider = _RaisingGenerator()  # type: ignore[assignment]
    engine = HydeEngine(generator=gen, cache=_make_cache())
    result = await engine.expand("offsite backup failed")
    assert result.pseudo_doc is None
    assert result.cache_hit is False
    assert result.generation_ms is None


class _SlowGenerator:
    """Generator that sleeps before responding so generation_ms is
    measurably > 0 even on fast CI."""

    provider: str = "slow"
    model: str = "slow-1"
    _sleep_seconds = 0.02

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
        await asyncio.sleep(self._sleep_seconds)
        return GenerationResult(text="slow doc", finish_reason="stop", model=self.model)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_generation_ms_reflects_real_wall_clock() -> None:
    """A generator that sleeps 20ms must yield generation_ms ≥ 15ms
    (small slack for sleep granularity). Pinning this catches the
    common refactor mistake of measuring before the await instead
    of around it."""
    gen: GeneratorProvider = _SlowGenerator()  # type: ignore[assignment]
    engine = HydeEngine(generator=gen, cache=_make_cache())
    result = await engine.expand("offsite backup failed")
    assert result.pseudo_doc == "slow doc"
    assert result.generation_ms is not None
    assert result.generation_ms >= 15.0, (
        f"generation_ms {result.generation_ms} < 15ms despite a "
        f"~20ms sleep in the fake generator. The wrap-around-await "
        f"timing pattern probably regressed."
    )
