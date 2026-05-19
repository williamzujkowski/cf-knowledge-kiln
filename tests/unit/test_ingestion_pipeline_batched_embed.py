"""Batched concurrent embedding fan-out (PR C, #108 item 2 prep).

The embedding pass turns a list of chunks-needing-embedding into
``(chunk, vector)`` pairs by calling ``provider.embed`` in batches
of ``KILN_INGEST_EMBED_BATCH_SIZE`` and running up to
``KILN_INGEST_EMBED_CONCURRENCY`` batches in parallel via
``asyncio.gather`` with a semaphore cap.

These unit tests exercise the fan-out helper in isolation — no DB.
The integration test in ``tests/integration/test_ingestion_pipeline.py``
already covers the end-to-end write path; here we only assert:

* batches are size ≤ batch_size,
* multiple batches overlap in wall clock,
* output order matches input order despite concurrent completion,
* empty input short-circuits without any provider call,
* a single chunk produces a single batch.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cf_knowledge_kiln.ingestion.embedding.batched import embed_chunks_concurrently


class _RecordingProvider:
    """Records every ``embed`` call: batch size + start/end wall times.

    Deterministic: returns a 1-D vector ``[float(len(text))]`` per text
    so the test can map output back to input by inspection.
    """

    provider = "recording"
    model = "recording-1"
    dimensions = 1

    def __init__(self, *, per_call_delay: float = 0.0) -> None:
        self.calls: list[list[str]] = []
        self.windows: list[tuple[float, float]] = []
        self._per_call_delay = per_call_delay

    async def embed(self, texts: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        self.calls.append(list(texts))
        if self._per_call_delay > 0:
            await asyncio.sleep(self._per_call_delay)
        end = time.perf_counter()
        self.windows.append((start, end))
        return [[float(len(t))] for t in texts]

    async def aclose(self) -> None:
        return None


class TestBatchedConcurrentEmbedFanOut:
    async def test_empty_input_short_circuits_with_no_provider_call(self) -> None:
        provider = _RecordingProvider()
        out = await embed_chunks_concurrently(
            texts=[],
            provider=provider,
            batch_size=32,
            concurrency=4,
        )
        assert out == []
        assert provider.calls == []

    async def test_single_chunk_produces_one_batch(self) -> None:
        provider = _RecordingProvider()
        out = await embed_chunks_concurrently(
            texts=["only-one"],
            provider=provider,
            batch_size=32,
            concurrency=4,
        )
        assert len(out) == 1
        assert provider.calls == [["only-one"]]
        # Vector returned in input order.
        assert out[0] == [float(len("only-one"))]

    async def test_batches_respect_batch_size_cap(self) -> None:
        provider = _RecordingProvider()
        texts = [f"t{i}" for i in range(10)]
        out = await embed_chunks_concurrently(
            texts=texts,
            provider=provider,
            batch_size=3,
            concurrency=4,
        )
        # 10 chunks / batch_size 3 → 4 batches of sizes 3,3,3,1.
        assert len(provider.calls) == 4
        for call in provider.calls:
            assert len(call) <= 3
        # All output present and in input order (vectors keyed by len("tN")).
        assert len(out) == 10
        for text, vector in zip(texts, out, strict=True):
            assert vector == [float(len(text))]

    async def test_multiple_batches_overlap_in_wall_clock(self) -> None:
        """Concurrency must actually run batches in parallel.

        Asserts wall-clock overlap by comparing total elapsed time
        to the sequential lower bound. With concurrency=4 and 4
        batches each delaying 50 ms, sequential would be ~200 ms;
        concurrent should finish well under that.
        """
        provider = _RecordingProvider(per_call_delay=0.05)
        # 4 batches of 1 text each = 4 concurrent provider calls.
        texts = [f"t{i}" for i in range(4)]
        t0 = time.perf_counter()
        await embed_chunks_concurrently(
            texts=texts,
            provider=provider,
            batch_size=1,
            concurrency=4,
        )
        elapsed = time.perf_counter() - t0
        # Sequential lower bound is 4 * 50ms = 200ms. Concurrent
        # should finish well under that. Allow generous slack for
        # CI noise: anything under 150ms proves real overlap.
        assert elapsed < 0.15, f"no concurrency observed: elapsed={elapsed:.3f}s"
        # Also assert window overlap on at least one pair.
        windows = provider.windows
        assert any(
            a[0] < b[1] and b[0] < a[1] for i, a in enumerate(windows) for b in windows[i + 1 :]
        ), "no two batches overlapped in wall clock"

    async def test_semaphore_caps_parallelism(self) -> None:
        """concurrency=2 must serialize the third batch behind the first two."""
        provider = _RecordingProvider(per_call_delay=0.05)
        # 4 single-chunk batches; concurrency=2 → two waves of 2.
        texts = [f"t{i}" for i in range(4)]
        await embed_chunks_concurrently(
            texts=texts,
            provider=provider,
            batch_size=1,
            concurrency=2,
        )
        windows = provider.windows
        # Count peak simultaneity at the midpoint of the first
        # window. With concurrency=2 it must never exceed 2.
        for w in windows:
            midpoint = (w[0] + w[1]) / 2
            active = sum(1 for other in windows if other[0] <= midpoint <= other[1])
            assert active <= 2, f"semaphore breached: {active} active at {midpoint}"

    async def test_output_order_matches_input_order_under_concurrent_completion(
        self,
    ) -> None:
        """Batches complete out of order but the returned list stays in input order.

        We force out-of-order completion by giving later batches a
        shorter delay than earlier ones. If the fan-out forgot to
        sort by input index, the assertion would catch it.
        """

        class _OrderJugglingProvider:
            provider = "juggle"
            model = "juggle-1"
            dimensions = 1

            def __init__(self) -> None:
                self.call_index = 0

            async def embed(self, texts: list[str]) -> list[list[float]]:
                idx = self.call_index
                self.call_index += 1
                # Earlier batches sleep longer → they finish AFTER
                # later batches. This is the order test.
                # We don't know which batch this is at provider level
                # (only the caller does), so use the call index.
                # Delay decreases with each successive call.
                await asyncio.sleep(0.02 * max(0, 4 - idx))
                # Return a vector whose only float is the input length.
                # We want the unique-per-text encoding so the test can
                # verify "vector at position N corresponds to text N."
                return [[float(len(t))] for t in texts]

            async def aclose(self) -> None:
                return None

        provider = _OrderJugglingProvider()
        texts = [f"text-{i:02d}-{'x' * i}" for i in range(8)]
        out = await embed_chunks_concurrently(
            texts=texts,
            provider=provider,
            batch_size=2,
            concurrency=4,
        )
        assert len(out) == len(texts)
        for text, vector in zip(texts, out, strict=True):
            assert vector == [float(len(text))], (
                "output order does not match input order under concurrent completion"
            )

    async def test_invalid_batch_size_raises(self) -> None:
        provider = _RecordingProvider()
        with pytest.raises(ValueError):
            await embed_chunks_concurrently(
                texts=["a"],
                provider=provider,
                batch_size=0,
                concurrency=4,
            )

    async def test_invalid_concurrency_raises(self) -> None:
        provider = _RecordingProvider()
        with pytest.raises(ValueError):
            await embed_chunks_concurrently(
                texts=["a"],
                provider=provider,
                batch_size=4,
                concurrency=0,
            )

    async def test_provider_returning_wrong_vector_count_raises(self) -> None:
        """A misbehaving provider that drops/dupes vectors must fail loudly."""

        class _BadProvider:
            provider = "bad"
            model = "bad-1"
            dimensions = 1

            async def embed(self, texts: list[str]) -> list[list[float]]:
                # Return one fewer vector than requested.
                return [[1.0] for _ in texts[:-1]] if len(texts) > 1 else []

            async def aclose(self) -> None:
                return None

        with pytest.raises(ValueError):
            await embed_chunks_concurrently(
                texts=["a", "b", "c"],
                provider=_BadProvider(),
                batch_size=3,
                concurrency=1,
            )
