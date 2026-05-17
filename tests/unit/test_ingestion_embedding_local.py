"""Unit tests for the local sentence-transformers embedding adapter.

We don't load the real model here — that would download ~500 MB of
weights and is gated behind an integration test. Instead, the adapter
takes a ``model_factory`` callable that returns the encoder, and these
tests inject a fake encoder so the contract can be exercised offline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cf_knowledge_kiln.ingestion.embedding.local import LocalEmbeddingProvider


class _FakeEncoder:
    """Stands in for ``sentence_transformers.SentenceTransformer``."""

    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions
        self.encode_calls = 0
        self.load_count = 1

    def encode(
        self,
        texts: list[str],
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = False,
    ) -> list[list[float]]:
        self.encode_calls += 1
        # Deterministic: bytes-of-text as the seed, padded to dimensions.
        vectors: list[list[float]] = []
        for t in texts:
            seed = sum(t.encode("utf-8")) or 1
            vectors.append([(seed % 7) / 7.0] * self._dimensions)
        return vectors


def _factory(dimensions: int) -> tuple[Any, list[str]]:
    """Return (factory, calls) so tests can assert the model name passed."""
    calls: list[str] = []

    def make(name: str) -> _FakeEncoder:
        calls.append(name)
        return _FakeEncoder(dimensions)

    return make, calls


class TestLocalEmbeddingProvider:
    async def test_lazy_load_on_first_embed(self) -> None:
        make, calls = _factory(8)
        provider = LocalEmbeddingProvider(
            model="nomic-embed-text-v1.5",
            dimensions=8,
            model_factory=make,
        )
        # Constructor must not load the model — that's expensive.
        assert calls == []
        await provider.embed(["hello"])
        assert calls == ["nomic-embed-text-v1.5"]

    async def test_subsequent_embeds_reuse_the_loaded_model(self) -> None:
        make, calls = _factory(8)
        provider = LocalEmbeddingProvider(
            model="nomic-embed-text-v1.5", dimensions=8, model_factory=make
        )
        await provider.embed(["a"])
        await provider.embed(["b"])
        await provider.embed(["c"])
        # Loaded once, used thrice.
        assert len(calls) == 1

    async def test_empty_input_skips_encoder_entirely(self) -> None:
        make, calls = _factory(8)
        provider = LocalEmbeddingProvider(model="x", dimensions=8, model_factory=make)
        assert await provider.embed([]) == []
        # Empty input must not trigger a lazy load either.
        assert calls == []

    async def test_returns_dim_correct_vectors(self) -> None:
        make, _ = _factory(384)
        provider = LocalEmbeddingProvider(model="x", dimensions=384, model_factory=make)
        vectors = await provider.embed(["alpha", "beta"])
        assert len(vectors) == 2
        assert all(len(v) == 384 for v in vectors)

    async def test_provider_and_model_metadata(self) -> None:
        make, _ = _factory(8)
        provider = LocalEmbeddingProvider(
            model="nomic-embed-text-v1.5", dimensions=8, model_factory=make
        )
        assert provider.provider == "local"
        assert provider.model == "nomic-embed-text-v1.5"
        assert provider.dimensions == 8

    async def test_encode_runs_off_the_event_loop(self) -> None:
        """The encoder must run in a worker thread, not block the loop.

        We assert this by having the encoder block on a synchronous
        ``time.sleep`` while the loop continues to schedule other tasks.
        """
        import time

        class _BlockingEncoder:
            def encode(self, texts: list[str], **_kwargs: Any) -> list[list[float]]:
                time.sleep(0.1)
                return [[0.0] * 4 for _ in texts]

        provider = LocalEmbeddingProvider(
            model="x", dimensions=4, model_factory=lambda _name: _BlockingEncoder()
        )
        counter = 0

        async def tick() -> None:
            nonlocal counter
            for _ in range(20):
                counter += 1
                await asyncio.sleep(0.01)

        # If embed blocked the loop, `tick` couldn't advance during the 0.1s sleep.
        await asyncio.gather(provider.embed(["a"]), tick())
        assert counter >= 5

    async def test_encoder_dim_mismatch_raises(self) -> None:
        class _WrongDimEncoder:
            def encode(self, texts: list[str], **_kwargs: Any) -> list[list[float]]:
                return [[0.0] * 16 for _ in texts]

        provider = LocalEmbeddingProvider(
            model="x", dimensions=8, model_factory=lambda _name: _WrongDimEncoder()
        )
        with pytest.raises(ValueError, match="dimensions"):
            await provider.embed(["hi"])
