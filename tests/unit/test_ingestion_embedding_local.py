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
    """Return (factory, calls) so tests can assert the model name passed.

    ``make`` accepts ``**kwargs`` so the injectable model-factory
    contract can grow new keywords (e.g. ``trust_remote_code``) without
    breaking every test double — a test double should be liberal in
    what it accepts.
    """
    calls: list[str] = []

    def make(name: str, device: str | None = None, **kwargs: Any) -> _FakeEncoder:
        calls.append(name)
        # Stash device + any other factory kwargs on the encoder so
        # tests can introspect them.
        enc = _FakeEncoder(dimensions)
        enc.device = device  # type: ignore[attr-defined]
        enc.factory_kwargs = kwargs  # type: ignore[attr-defined]
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
            model_factory=lambda _name, **_: _BlockingEncoder(),
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
            model_factory=lambda _name, **_: _WrongDimEncoder(),
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

        def make(name: str, device: str | None = None, **_: Any) -> _FakeEncoder:
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

        def make(name: str, device: str | None = None, **_: Any) -> _FakeEncoder:
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

        def make(name: str, device: str | None = None, **_: Any) -> _FakeEncoder:
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


class TestTrustRemoteCode:
    """``trust_remote_code`` must be config-driven, not hardcoded.

    Nomic Embed v1.5 ships custom modeling code (``nomic-bert-2048``)
    and needs ``trust_remote_code=True`` to load under modern
    ``transformers``. Most other sentence-transformers models do not.
    Hardcoding the flag would couple the adapter to one model family;
    omitting it (the pre-fix state) makes the configured default model
    fail to load in production. The flag therefore belongs in config,
    defaulting to ``False`` so running remote code is an explicit
    opt-in.
    """

    async def test_trust_remote_code_forwarded_to_factory(self) -> None:
        make, _ = _factory(4)
        provider = LocalSentenceTransformersProvider(
            model_name="x",
            dimensions=4,
            trust_remote_code=True,
            model_factory=make,
        )
        await provider.embed(["one"])
        encoder = provider._encoder  # test-only introspection
        assert encoder is not None
        assert encoder.factory_kwargs["trust_remote_code"] is True

    async def test_trust_remote_code_defaults_to_false(self) -> None:
        make, _ = _factory(4)
        provider = LocalSentenceTransformersProvider(
            model_name="x", dimensions=4, model_factory=make
        )
        assert provider.trust_remote_code is False
        await provider.embed(["one"])
        encoder = provider._encoder
        assert encoder is not None
        assert encoder.factory_kwargs["trust_remote_code"] is False

    def test_default_factory_forwards_trust_remote_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_default_factory`` threads the flag into ``SentenceTransformer``."""
        import sys
        import types

        recorded: dict[str, Any] = {}

        class _RecordingST:
            def __init__(self, name: str, device: str | None = None, **kwargs: Any) -> None:
                recorded["name"] = name
                recorded["device"] = device
                recorded.update(kwargs)

        fake_mod = types.ModuleType("sentence_transformers")
        fake_mod.SentenceTransformer = _RecordingST  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

        from cf_knowledge_kiln.ingestion.embedding.local import _default_factory

        _default_factory("nomic-ai/nomic-embed-text-v1.5", trust_remote_code=True)
        assert recorded["trust_remote_code"] is True


class TestModelFamilyRequiredDeps:
    """#231: Nomic family needs ``einops``; missing it must fail-fast.

    Background: Nomic's custom modeling code (``nomic-bert-2048``)
    imports ``einops``, which is NOT declared by ``sentence-transformers``
    or ``transformers``. Before this guard the worker spun loading the
    model with a silent ``[transformers] Encountered exception while
    importing einops`` for ~30 cycles per #228 incident.
    """

    def test_nomic_without_einops_raises_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nomic-name + no einops → ImportError before any model load."""
        import sys

        saved = sys.modules.pop("einops", None)
        monkeypatch.setitem(sys.modules, "einops", None)
        try:
            with pytest.raises(ImportError, match="einops"):
                LocalSentenceTransformersProvider(
                    model_name="nomic-ai/nomic-embed-text-v1.5",
                    dimensions=768,
                    trust_remote_code=True,
                )
        finally:
            sys.modules.pop("einops", None)
            if saved is not None:
                sys.modules["einops"] = saved

    def test_nomic_with_einops_present_constructs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If einops IS importable, the guard passes through.

        We inject a fake einops module so the test doesn't depend on
        the real package being installed in the test environment.
        """
        import sys
        import types

        fake = types.ModuleType("einops")
        monkeypatch.setitem(sys.modules, "einops", fake)
        # ``model_factory`` skips the real load path entirely, but the
        # guard runs even before the factory is consulted… so this
        # test specifically targets the guard's pass-through. Use the
        # default factory so the guard is reached, but inject the
        # model factory via the keyword to avoid an actual download.
        make, _ = _factory(768)
        # Note: when model_factory is provided, the guard is skipped
        # (test-only contract). To verify the guard's PASS path we
        # construct WITHOUT model_factory and let it reach
        # _check_required_deps, then stop before _default_factory by
        # never calling embed(). Constructor must not raise.
        LocalSentenceTransformersProvider(
            model_name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            trust_remote_code=True,
            model_factory=make,  # still injected so embed() wouldn't fail later
        )
        # Also assert the guard runs in the no-factory path: rebuild
        # with no factory but with einops present → still must not
        # raise at construction.
        LocalSentenceTransformersProvider(
            model_name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            trust_remote_code=True,
        )

    def test_non_nomic_model_skips_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """e5 (or any model not in the deps table) constructs without einops."""
        import sys

        saved = sys.modules.pop("einops", None)
        monkeypatch.setitem(sys.modules, "einops", None)
        try:
            # No raise — e5 doesn't trigger the einops check.
            LocalSentenceTransformersProvider(
                model_name="intfloat/e5-small-v2",
                dimensions=384,
            )
        finally:
            sys.modules.pop("einops", None)
            if saved is not None:
                sys.modules["einops"] = saved

    def test_test_factory_path_skips_guard(self) -> None:
        """Tests inject ``model_factory`` — they shouldn't need einops to construct."""
        # No einops manipulation: the test injection path bypasses the
        # check entirely. Without this contract, every test against
        # the Nomic model name would need einops installed.
        make, _ = _factory(768)
        LocalSentenceTransformersProvider(
            model_name="nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            trust_remote_code=True,
            model_factory=make,
        )


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


# ─── #204 — model-family text prefixes ────────────────────────────────


class _TextCapturingEncoder:
    """Fake encoder that captures the exact texts passed to ``encode``.

    Existing :class:`_FakeEncoder` discards texts; these tests need the
    raw input to assert prefix application.
    """

    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions
        self.encoded_batches: list[list[str]] = []

    def encode(
        self,
        texts: list[str],
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = False,
        batch_size: int = 32,
    ) -> list[list[float]]:
        self.encoded_batches.append(list(texts))
        return [[0.0] * self._dimensions for _ in texts]


def _capturing_factory(dimensions: int) -> tuple[Any, _TextCapturingEncoder]:
    encoder = _TextCapturingEncoder(dimensions)

    def factory(_name: str, **_kwargs: Any) -> _TextCapturingEncoder:
        return encoder

    return factory, encoder


class TestModelFamilyPrefixes:
    """#204: e5 + Nomic models need passage/query prefixes or cosine scores collapse."""

    async def test_e5_small_v2_documents_get_passage_prefix(self) -> None:
        factory, encoder = _capturing_factory(384)
        provider = LocalSentenceTransformersProvider(
            "intfloat/e5-small-v2", dimensions=384, model_factory=factory
        )
        await provider.embed_documents(["first doc", "second doc"])
        assert encoder.encoded_batches == [["passage: first doc", "passage: second doc"]]

    async def test_e5_small_v2_query_gets_query_prefix(self) -> None:
        factory, encoder = _capturing_factory(384)
        provider = LocalSentenceTransformersProvider(
            "intfloat/e5-small-v2", dimensions=384, model_factory=factory
        )
        await provider.embed_query("what is the alert response")
        assert encoder.encoded_batches == [["query: what is the alert response"]]

    async def test_e5_base_v2_also_prefixed(self) -> None:
        """Pattern is family-wide, not version-specific."""
        factory, encoder = _capturing_factory(768)
        provider = LocalSentenceTransformersProvider(
            "intfloat/e5-base-v2", dimensions=768, model_factory=factory
        )
        await provider.embed_documents(["x"])
        assert encoder.encoded_batches == [["passage: x"]]

    async def test_multilingual_e5_large_also_prefixed(self) -> None:
        factory, encoder = _capturing_factory(1024)
        provider = LocalSentenceTransformersProvider(
            "intfloat/multilingual-e5-large", dimensions=1024, model_factory=factory
        )
        await provider.embed_documents(["x"])
        assert encoder.encoded_batches == [["passage: x"]]

    async def test_nomic_embed_v1_5_documents_get_search_document_prefix(self) -> None:
        factory, encoder = _capturing_factory(768)
        provider = LocalSentenceTransformersProvider(
            "nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            model_factory=factory,
        )
        await provider.embed_documents(["alpha", "beta"])
        assert encoder.encoded_batches == [["search_document: alpha", "search_document: beta"]]

    async def test_nomic_embed_v1_5_query_gets_search_query_prefix(self) -> None:
        factory, encoder = _capturing_factory(768)
        provider = LocalSentenceTransformersProvider(
            "nomic-ai/nomic-embed-text-v1.5",
            dimensions=768,
            model_factory=factory,
        )
        await provider.embed_query("alpha?")
        assert encoder.encoded_batches == [["search_query: alpha?"]]

    async def test_unknown_model_gets_no_prefix(self) -> None:
        """Models without a known family entry pass through unchanged.

        Avoids over-fitting: a future model that adopts its own prefix
        convention is silently wrong rather than silently right.
        """
        factory, encoder = _capturing_factory(384)
        provider = LocalSentenceTransformersProvider(
            "Snowflake/snowflake-arctic-embed-m",
            dimensions=384,
            model_factory=factory,
        )
        await provider.embed_documents(["unprefixed text"])
        assert encoder.encoded_batches == [["unprefixed text"]]

    async def test_raw_embed_remains_unprefixed(self) -> None:
        """Backward-compat path: ``embed()`` is raw, no prefix injection.

        Lets the startup health probe + any legacy caller exercise the
        encoder without the model-family branching.
        """
        factory, encoder = _capturing_factory(384)
        provider = LocalSentenceTransformersProvider(
            "intfloat/e5-small-v2", dimensions=384, model_factory=factory
        )
        await provider.embed(["raw text"])
        assert encoder.encoded_batches == [["raw text"]]

    async def test_empty_input_to_embed_documents_short_circuits(self) -> None:
        factory, encoder = _capturing_factory(384)
        provider = LocalSentenceTransformersProvider(
            "intfloat/e5-small-v2", dimensions=384, model_factory=factory
        )
        result = await provider.embed_documents([])
        assert result == []
        assert encoder.encoded_batches == []


def test_prefixes_for_table_shape() -> None:
    """Lock the prefix-lookup contract so additions are explicit."""
    from cf_knowledge_kiln.ingestion.embedding.local import _prefixes_for

    # Known families.
    assert _prefixes_for("intfloat/e5-small-v2") == ("passage: ", "query: ")
    assert _prefixes_for("nomic-ai/nomic-embed-text-v1.5") == (
        "search_document: ",
        "search_query: ",
    )
    # Unknown → empty pair (caller treats as no-prefix).
    assert _prefixes_for("unknown/model-name") == ("", "")
    # Empty model name (defensive).
    assert _prefixes_for("") == ("", "")
