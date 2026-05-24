"""Unit tests for the EmbeddingProvider protocol + deterministic mock.

Issue: #16. The mock is the thing every other unit test in Phase 4+
talks to so the suite never depends on a network or model weights.
The contract it has to honor:

* ``embed`` is async, takes a list of strings, returns a list of
  equal-length float vectors.
* ``dimensions`` is a positive int and matches what ``embed`` returns.
* The same input always yields the same vector (deterministic).
* Different inputs almost-always yield different vectors. We don't
  *require* perfect uniqueness — a hash collision is theoretically
  possible — but the seed used (sha256) makes it vanishingly unlikely
  for any realistic test corpus.
"""

from __future__ import annotations

import math

import pytest

from cf_knowledge_kiln.ingestion.embedding import (
    EmbeddingProvider,
    MockEmbeddingProvider,
)


def _is_unit_vector(v: list[float], tol: float = 1e-6) -> bool:
    norm = math.sqrt(sum(x * x for x in v))
    return abs(norm - 1.0) < tol


class TestMockEmbeddingProvider:
    """Behavioral contract for the deterministic mock."""

    async def test_implements_protocol(self) -> None:
        mock = MockEmbeddingProvider()
        assert isinstance(mock, EmbeddingProvider)

    async def test_dimensions_attribute_matches_output(self) -> None:
        mock = MockEmbeddingProvider(dimensions=384)
        [vector] = await mock.embed(["hello"])
        assert mock.dimensions == 384
        assert len(vector) == 384

    async def test_default_dimensions_match_active_mvp_model(self) -> None:
        """768 is the nomic-embed-text-v1.5 dimensionality (ADR-0005)."""
        mock = MockEmbeddingProvider()
        assert mock.dimensions == 768

    async def test_provider_and_model_metadata_are_set(self) -> None:
        mock = MockEmbeddingProvider()
        # These are the strings persisted on chunk_embeddings rows.
        assert mock.provider == "mock"
        assert mock.model.startswith("mock-")

    async def test_empty_input_returns_empty_output(self) -> None:
        mock = MockEmbeddingProvider()
        assert await mock.embed([]) == []

    async def test_deterministic_same_input_same_vector(self) -> None:
        mock = MockEmbeddingProvider()
        [v1] = await mock.embed(["consistent text"])
        [v2] = await mock.embed(["consistent text"])
        assert v1 == v2

    async def test_different_inputs_give_different_vectors(self) -> None:
        mock = MockEmbeddingProvider()
        v_a, v_b = await mock.embed(["alpha", "beta"])
        assert v_a != v_b

    async def test_batch_matches_individual_calls(self) -> None:
        """Calling embed in a batch must equal calling it one-at-a-time."""
        mock = MockEmbeddingProvider()
        batch = await mock.embed(["alpha", "beta", "gamma"])
        [a] = await mock.embed(["alpha"])
        [b] = await mock.embed(["beta"])
        [g] = await mock.embed(["gamma"])
        assert batch == [a, b, g]

    async def test_returns_unit_vectors(self) -> None:
        """Unit-norm vectors keep cosine-similarity math straightforward."""
        mock = MockEmbeddingProvider()
        vectors = await mock.embed(["one", "two", "three"])
        for v in vectors:
            assert _is_unit_vector(v)

    async def test_rejects_non_positive_dimensions(self) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            MockEmbeddingProvider(dimensions=0)
        with pytest.raises(ValueError, match="dimensions"):
            MockEmbeddingProvider(dimensions=-1)

    async def test_custom_model_string_is_preserved(self) -> None:
        """Tests that simulate provider-swapping need to override model."""
        mock = MockEmbeddingProvider(model="mock-768-test")
        assert mock.model == "mock-768-test"

    async def test_embed_documents_matches_embed_for_mock(self) -> None:
        """#204: mock provider's embed_documents trivially delegates to embed."""
        mock = MockEmbeddingProvider()
        a = await mock.embed_documents(["x", "y"])
        b = await mock.embed(["x", "y"])
        assert a == b

    async def test_embed_query_returns_single_vector(self) -> None:
        """#204: embed_query returns one vector, not a list-of-one."""
        mock = MockEmbeddingProvider()
        vec = await mock.embed_query("test query")
        assert isinstance(vec, list)
        # Each element is a float dimension, not a nested list.
        assert all(isinstance(x, float) for x in vec)
        assert len(vec) == mock.dimensions
