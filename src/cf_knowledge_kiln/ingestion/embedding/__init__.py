"""Embedding-provider abstraction.

Defines the :class:`EmbeddingProvider` Protocol and a deterministic
:class:`MockEmbeddingProvider` for tests. Real adapters live in
sibling modules:

* :mod:`cf_knowledge_kiln.ingestion.embedding.openai_compatible` — HTTP
  adapter speaking the OpenAI ``/v1/embeddings`` shape.
* :mod:`cf_knowledge_kiln.ingestion.embedding.local` — process-local
  adapter using ``sentence-transformers``.

The factory in :mod:`cf_knowledge_kiln.ingestion.embedding.factory`
picks one based on ``config/models.yaml``. Selection is config, not
code (ADR-0005).

Protocol contract:

* ``embed_documents(texts) -> list[list[float]]`` — async, embed
  texts as **passages** (i.e. documents being indexed). Model-aware
  prefixes (``passage: `` for e5, ``search_document: `` for Nomic)
  are applied here when the underlying model requires them.
* ``embed_query(text) -> list[float]`` — async, embed a single text
  as a **query**. Mirrors ``embed_documents`` but uses the model's
  query-side prefix (``query: ``, ``search_query: ``). Returns one
  vector, not a list-of-one.
* ``embed(texts) -> list[list[float]]`` — async, raw passthrough
  (no prefix). Retained for backward compat and for callers that
  genuinely don't have a passage/query distinction (e.g. the
  startup health probe). New code should prefer the explicit
  ``embed_documents`` / ``embed_query`` methods (#204).
* ``dimensions`` — positive int, fixed for the lifetime of the
  provider. Persisted on every ``chunk_embeddings`` row.
* ``model`` / ``provider`` — strings persisted alongside the vector so
  Phase 5 retrieval can filter by them.
* ``aclose()`` — release HTTP / model resources. Optional; the mock
  is a no-op.

#204: e5-family and Nomic Embed v1.5 require model-specific text
prefixes to produce the cosine-similarity range their downstream
thresholds were calibrated for. Calling ``encode(["raw text"])`` on
e5-small-v2 collapses scores into the 0.1-0.4 band; the right way is
``encode(["passage: raw text"])`` (for docs) or
``encode(["query: raw text"])`` (for queries). The Local provider
applies these per-model-family in ``embed_documents`` /
``embed_query``; HTTP-API providers (OpenAI-compatible) and the Mock
provider don't need them and pass through.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol, runtime_checkable

DEFAULT_DIMENSIONS = 768  # nomic-embed-text-v1.5 (ADR-0005)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Swappable embedding backend.

    Implementations: :class:`MockEmbeddingProvider`,
    ``OpenAICompatibleEmbeddingProvider``, ``LocalEmbeddingProvider``.
    """

    provider: str
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


class MockEmbeddingProvider:
    """Deterministic in-process provider for tests.

    Vectors are derived from ``sha256(text)`` so the same input always
    produces the same vector across processes and machines — useful
    for assertions like "re-ingestion of unchanged content writes the
    same row." Vectors are L2-normalized, which keeps cosine-similarity
    math clean in retrieval tests.

    No network, no model weights, no external dependencies.
    """

    provider = "mock"

    def __init__(
        self,
        *,
        dimensions: int = DEFAULT_DIMENSIONS,
        model: str | None = None,
    ) -> None:
        if dimensions <= 0:
            raise ValueError(f"dimensions must be positive, got {dimensions}")
        self.dimensions = dimensions
        self.model = model or f"mock-{dimensions}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector_for(text) for text in texts]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Mock: no prefix needed; delegate to :meth:`embed`."""
        return await self.embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        """Mock: no prefix needed; delegate to :meth:`embed`."""
        return (await self.embed([text]))[0]

    async def aclose(self) -> None:
        return None

    def _vector_for(self, text: str) -> list[float]:
        """Derive a deterministic unit vector from ``text``.

        Stretches ``sha256(text)`` into ``dimensions`` uint32s, maps
        each into the open interval ``(-1, 1)``, then L2-normalizes.
        Uniform-ish in expectation and always finite, so normalization
        is well-defined.
        """
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        floats: list[float] = []
        counter = 0
        while len(floats) < self.dimensions:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for i in range(0, len(block), 4):
                if len(floats) >= self.dimensions:
                    break
                (u,) = struct.unpack(">I", block[i : i + 4])
                # Map [0, 2**32) -> (-1, 1).
                floats.append(u / 2_147_483_648.0 - 1.0)
            counter += 1
        norm = math.sqrt(sum(x * x for x in floats))
        if norm == 0.0:
            floats[0] = 1.0
            norm = 1.0
        return [x / norm for x in floats]


# Re-export every concrete provider so callers can write
# ``from cf_knowledge_kiln.ingestion.embedding import <Provider>``
# without reaching into a submodule. The subpath imports (``.local``,
# ``.openai_compatible``) also still work. Phase 4's
# ``LocalEmbeddingProvider`` symbol is preserved as a symbol-level
# alias by ``local.py``; we re-export both. All three real providers
# are surfaced here for symmetry — the factory remains the *selection*
# entry point, but direct construction (tests, embedded use) should
# not have to know which submodule a provider lives in.
from cf_knowledge_kiln.ingestion.embedding.local import (  # noqa: E402
    LocalEmbeddingProvider,
    LocalSentenceTransformersProvider,
)
from cf_knowledge_kiln.ingestion.embedding.openai_compatible import (  # noqa: E402
    OpenAICompatibleEmbeddingProvider,
)

__all__ = [
    "DEFAULT_DIMENSIONS",
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "LocalSentenceTransformersProvider",
    "MockEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
]
