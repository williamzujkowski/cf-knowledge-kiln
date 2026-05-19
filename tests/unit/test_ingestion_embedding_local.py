"""Unit tests for the local sentence-transformers embedding adapter.

We don't load the real model here — that would download ~500 MB of
weights and is gated behind an integration test. Instead, the adapter
takes a ``model_factory`` callable that returns the encoder, and these
tests inject a fake encoder so the contract can be exercised offline.

The class is :class:`LocalSentenceTransformersProvider`. The previous
name :class:`LocalEmbeddingProvider` remains as a back-compat alias so
existing imports keep working; both names refer to the same class.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cf_knowledge_kiln.ingestion.embedding.local import (
    LocalEmbeddingProvider,
    LocalSentenceTransformersProvider,
)


class _FakeEncoder:
    """Stands in for ``sentence_transformers.SentenceTransformer``."""

    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions
        self.encode_calls = 0
        self.encode_kwargs: list[dict[str, Any]] = []
        self.load_count = 1

    def encode(
        self,
        texts: list[str],
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = False,
        batch_size: int = 32,
    ) -> list[list[float]]:
        self.encode_calls += 1
        self.encode_kwargs.append(
            {
                "normalize_embeddings": normalize_embeddings,
                "convert_to_numpy": convert_to_numpy,
                "batch_size": batch_size,
            }
        )
        # Deterministic: bytes-of-text as the seed, padded to dimensions.
        vectors: list[list[float]] = []
        for t in texts:
            seed = sum(t.encode("utf-8")) or 1
            vectors.append([(seed % 7) / 7.0] * self._dimensions)
        return vectors


def _factory(dimensions: int) -> tuple[Any, list[str]]:
    """Return (factory, calls) so tests can assert the model name passed."""
    calls: list[str] = []

    def make(name: str, device: str | None = None) -> _FakeEncoder:
        calls.append(name)
        # Stash device on the encoder so tests can introspect it.
        enc = _FakeEncoder(dimensions)
        enc.device = device  # type: ignore[attr-defined]
        return enc

    return make, calls


class TestBackCompatAlias:
    """``LocalEmbeddingProvider`` must remain importable as an alias."""

    def test_alias_is_the_same_class(self) -> None:
        assert LocalEmbeddingProvider is LocalSentenceTransformersProvider


class TestLocalSentenceTransformersProvider:
    async def test_lazy_load_on_first_embed(self) -> None:
        make, calls = _factory(8)
        provider = LocalSentenceTransformersProvider(
            model_name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=8,
            model_factory=make,
        )
        # Constructor must not load the model — that's expensive.
        assert calls == []
        await provider.embed(["hello"])
        assert calls == ["nomic-ai/nomic-embed-text-v1.5"]

    async def test_subsequent_embeds_reuse_the_loaded_model(self) -> None:
        make, calls = _factory(8)
        provider = LocalSentenceTransformersProvider(
            model_name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=8,
            model_factory=make,
        )
        await provider.embed(["a"])
        await provider.embed(["b"])
        await provider.embed(["c"])
        # Loaded once, used thrice.
        assert len(calls) == 1

    async def test_empty_input_skips_encoder_entirely(self) -> None:
        make, calls = _factory(8)
        provider = LocalSentenceTransformersProvider(
            model_name="x", dimensions=8, model_factory=make
        )
        assert await provider.embed([]) == []
        # Empty input must not trigger a lazy load either.
        assert calls == []

    async def test_returns_dim_correct_vectors(self) -> None:
        make, _ = _factory(384)
        provider = LocalSentenceTransformersProvider(
            model_name="x", dimensions=384, model_factory=make
        )
        vectors = await provider.embed(["alpha", "beta"])
        assert len(vectors) == 2
        assert all(len(v) == 384 for v in vectors)

    async def test_provider_and_model_metadata(self) -> None:
        make, _ = _factory(8)
        provider = LocalSentenceTransformersProvider(
            model_name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=8,
            model_factory=make,
        )
        assert provider.provider == "local-sentence-transformers"
        # ``model`` is the canonical Protocol field; ``name`` is an alias.
        assert provider.model == "nomic-ai/nomic-embed-text-v1.5"
        assert provider.name == "nomic-ai/nomic-embed-text-v1.5"
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

        provider = LocalSentenceTransformersProvider(
            model_name="x",
            dimensions=4,
            model_factory=lambda _name, device=None: _BlockingEncoder(),
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

        provider = LocalSentenceTransformersProvider(
            model_name="x",
            dimensions=8,
            model_factory=lambda _name, device=None: _WrongDimEncoder(),
        )
        with pytest.raises(ValueError, match="dimensions"):
            await provider.embed(["hi"])

    async def test_batch_size_is_threaded_through_to_encode(self) -> None:
        """The configured batch_size must reach the underlying encoder."""
        make, _ = _factory(8)
        provider = LocalSentenceTransformersProvider(
            model_name="x",
            dimensions=8,
            batch_size=7,
            model_factory=make,
        )
        await provider.embed(["a", "b", "c"])
        # ``make`` returns the encoder; pull it back via the lazy slot.
        encoder = provider._encoder  # test-only introspection
        assert encoder is not None
        assert encoder.encode_kwargs[0]["batch_size"] == 7

    async def test_default_batch_size_is_32(self) -> None:
        make, _ = _factory(8)
        provider = LocalSentenceTransformersProvider(
            model_name="x", dimensions=8, model_factory=make
        )
        await provider.embed(["a"])
        encoder = provider._encoder
        assert encoder is not None
        assert encoder.encode_kwargs[0]["batch_size"] == 32

    async def test_device_param_is_forwarded_to_factory(self) -> None:
        """Operators select cpu/cuda/mps via the device argument."""
        seen: dict[str, str | None] = {}

        def make(name: str, device: str | None = None) -> _FakeEncoder:
            seen["device"] = device
            return _FakeEncoder(4)

        provider = LocalSentenceTransformersProvider(
            model_name="x",
            dimensions=4,
            device="mps",
            model_factory=make,
        )
        await provider.embed(["one"])
        assert seen["device"] == "mps"

    async def test_device_defaults_to_env_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``device=None``, fall back to ``KILN_EMBEDDING_DEVICE``."""
        monkeypatch.setenv("KILN_EMBEDDING_DEVICE", "cuda")
        seen: dict[str, str | None] = {}

        def make(name: str, device: str | None = None) -> _FakeEncoder:
            seen["device"] = device
            return _FakeEncoder(4)

        provider = LocalSentenceTransformersProvider(
            model_name="x", dimensions=4, model_factory=make
        )
        await provider.embed(["one"])
        assert seen["device"] == "cuda"

    async def test_device_defaults_to_cpu_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KILN_EMBEDDING_DEVICE", raising=False)
        seen: dict[str, str | None] = {}

        def make(name: str, device: str | None = None) -> _FakeEncoder:
            seen["device"] = device
            return _FakeEncoder(4)

        provider = LocalSentenceTransformersProvider(
            model_name="x", dimensions=4, model_factory=make
        )
        await provider.embed(["one"])
        assert seen["device"] == "cpu"

    async def test_constructor_rejects_non_positive_dimensions(self) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            LocalSentenceTransformersProvider(model_name="x", dimensions=0)
        with pytest.raises(ValueError, match="dimensions"):
            LocalSentenceTransformersProvider(model_name="x", dimensions=-1)

    async def test_constructor_rejects_non_positive_batch_size(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            LocalSentenceTransformersProvider(model_name="x", dimensions=8, batch_size=0)

    async def test_positional_args_work(self) -> None:
        """The constructor signature accepts positional model_name + dimensions."""
        make, _ = _factory(8)
        provider = LocalSentenceTransformersProvider(
            "nomic-ai/nomic-embed-text-v1.5",
            8,
            model_factory=make,
        )
        assert provider.model == "nomic-ai/nomic-embed-text-v1.5"
        assert provider.dimensions == 8


class TestMissingExtraImportError:
    """When ``sentence-transformers`` is absent, the error must be actionable."""

    def test_default_factory_raises_clear_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the extra isn't installed, the default factory raises ImportError."""
        # Simulate the missing module by stubbing sys.modules so the
        # `import sentence_transformers` inside the factory fails.
        import sys

        # Stash any preloaded module so we can restore it after the test.
        saved = sys.modules.pop("sentence_transformers", None)
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        try:
            from cf_knowledge_kiln.ingestion.embedding.local import (
                _default_factory,
            )

            with pytest.raises(ImportError, match="real-embeddings"):
                _default_factory("x")
        finally:
            sys.modules.pop("sentence_transformers", None)
            if saved is not None:
                sys.modules["sentence_transformers"] = saved
