"""Local sentence-transformers embedding adapter.

Loads a HuggingFace sentence-transformer model into the worker
process and runs ``encode`` calls on a worker thread (so the asyncio
loop stays free). The MVP model is ``nomic-embed-text-v1.5`` (Nomic AI,
US-origin, Apache 2.0 — see :file:`docs/model-providers.md`).

The heavy dependency (``sentence-transformers`` and its transitive
torch install) lives in the optional ``embeddings`` extra. Importing
this module without that extra installed raises a clear
``ImportError`` on the first ``embed`` call, not at import time, so
the rest of the package still loads in environments that only use
``openai-compatible`` or the mock.

The model is lazy-loaded on the first call so unit tests can construct
the provider without paying the load cost.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER_NAME = "local"

# Default model factory: imported lazily so the ``embeddings`` extra
# is only required at provider-use time, not at import time.
ModelFactory = Callable[[str], Any]


def _default_factory(name: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover — exercised only without the extra
        raise ImportError(
            "LocalEmbeddingProvider requires the 'embeddings' extra. "
            "Install with: pip install 'cf-knowledge-kiln[embeddings]'"
        ) from exc
    return SentenceTransformer(name)


class LocalEmbeddingProvider:
    """In-process embedding via sentence-transformers.

    Tests inject a ``model_factory`` so they don't pay for real model
    weights. Production uses the default factory which loads from
    HuggingFace (cached locally per platform conventions).
    """

    provider = PROVIDER_NAME

    def __init__(
        self,
        *,
        model: str,
        dimensions: int,
        model_factory: ModelFactory | None = None,
        normalize: bool = True,
    ) -> None:
        if dimensions <= 0:
            raise ValueError(f"dimensions must be positive, got {dimensions}")
        self.model = model
        self.dimensions = dimensions
        self._factory = model_factory or _default_factory
        self._normalize = normalize
        self._encoder: Any | None = None
        self._load_lock = asyncio.Lock()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        encoder = await self._ensure_loaded()
        vectors = await asyncio.to_thread(self._encode_sync, encoder, texts)
        for v in vectors:
            if len(v) != self.dimensions:
                raise ValueError(
                    f"encoder returned {len(v)} dimensions, "
                    f"adapter is configured for {self.dimensions}"
                )
        return vectors

    async def aclose(self) -> None:
        # sentence-transformers doesn't expose a teardown hook; releasing
        # the reference lets the GC reclaim model memory when no other
        # holders remain.
        self._encoder = None

    async def _ensure_loaded(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        async with self._load_lock:
            if self._encoder is None:
                logger.info("loading local embedding model: %s", self.model)
                # Loading is CPU/GPU-heavy; keep the loop free.
                self._encoder = await asyncio.to_thread(self._factory, self.model)
        return self._encoder

    def _encode_sync(self, encoder: Any, texts: list[str]) -> list[list[float]]:
        result = encoder.encode(
            texts,
            normalize_embeddings=self._normalize,
            convert_to_numpy=False,
        )
        # SentenceTransformer can return numpy arrays or torch tensors;
        # coerce to plain python lists for downstream uniformity.
        return [list(map(float, v)) for v in result]


__all__ = ["PROVIDER_NAME", "LocalEmbeddingProvider"]
