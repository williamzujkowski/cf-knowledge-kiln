"""Local sentence-transformers embedding adapter.

Loads a HuggingFace sentence-transformer model into the worker
process and runs ``encode`` calls on a worker thread (so the asyncio
loop stays free). The MVP model is ``nomic-ai/nomic-embed-text-v1.5``
(Nomic AI, US-origin, Apache 2.0 — see :file:`docs/model-providers.md`).

This adapter is a *generic* wrapper around any sentence-transformers-
compatible HuggingFace model. Swapping the active model is a single
string change in ``config/models.yaml`` — no code change required.
For example, ``Snowflake/snowflake-arctic-embed-m`` is a drop-in
replacement (different dimensions; declare them in YAML).

Where the weights live: the underlying ``sentence-transformers`` /
``huggingface_hub`` libraries cache downloaded model weights under
``~/.cache/huggingface/`` by default (override with the standard
``HF_HOME`` env var). The kiln repo **never** commits model weights;
this adapter downloads on first use.

The heavy dependency (``sentence-transformers`` and its transitive
torch install) lives in the optional ``real-embeddings`` extra
(also aliased as ``embeddings`` for back-compat). Importing this
module without that extra installed raises a clear ``ImportError``
on the first ``embed`` call, not at import time, so the rest of the
package still loads in environments that only use ``openai-compatible``
or the mock.

The model is lazy-loaded on the first call so unit tests can construct
the provider without paying the load cost.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Canonical provider name. ``local`` is preserved as a back-compat alias
# in the factory's registry — both resolve to this class.
PROVIDER_NAME = "local-sentence-transformers"

DEFAULT_BATCH_SIZE = 32
DEFAULT_DEVICE = "cpu"
DEVICE_ENV_VAR = "KILN_EMBEDDING_DEVICE"

# Default model factory: imported lazily so the ``real-embeddings``
# extra is only required at provider-use time, not at import time.
# The factory accepts an optional ``device`` so callers can pin to
# ``cpu`` / ``cuda`` / ``mps`` without subclassing this module, plus a
# keyword-only ``trust_remote_code`` flag. Injected factories (tests)
# should accept ``**kwargs`` so this contract can grow new keywords
# without breaking every double.
ModelFactory = Callable[..., Any]


def _default_factory(
    name: str,
    device: str | None = None,
    *,
    trust_remote_code: bool = False,
) -> Any:
    """Instantiate a real ``SentenceTransformer`` for ``name``.

    Raises a clear ``ImportError`` with the install hint when the
    optional ``real-embeddings`` extra hasn't been installed. The error
    message names the install command exactly so operators don't have
    to guess.

    ``trust_remote_code`` is forwarded verbatim. Some models (notably
    ``nomic-ai/nomic-embed-text-v1.5``, which ships ``nomic-bert-2048``
    custom code) require it to load under modern ``transformers``;
    others must not have it. It is config-driven so the adapter stays
    model-agnostic — see :class:`LocalSentenceTransformersProvider`.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ImportError(
            "LocalSentenceTransformersProvider requires the "
            "'real-embeddings' extra. Install with: "
            "pip install -e '.[real-embeddings]'"
        ) from exc
    # ``device`` is a SentenceTransformer kwarg; ``None`` lets the
    # library pick (typically CPU if torch can't find an accelerator).
    return SentenceTransformer(name, device=device, trust_remote_code=trust_remote_code)


class LocalSentenceTransformersProvider:
    """In-process embedding via sentence-transformers.

    Generic wrapper: any sentence-transformers-compatible HuggingFace
    model works — swap ``model_name`` in ``config/models.yaml`` to
    change backends.

    Tests inject a ``model_factory`` so they don't pay for real model
    weights. Production uses the default factory which loads from
    HuggingFace (cached under ``~/.cache/huggingface/``).

    Parameters
    ----------
    model_name:
        Hugging Face model identifier (e.g. ``nomic-ai/nomic-embed-text-v1.5``).
        Also exposed as the :attr:`name` property.
    dimensions:
        Expected embedding dimensionality. Checked on first ``embed``
        call against the model's actual output; mismatched models
        raise ``ValueError`` so misconfiguration fails loudly instead
        of writing wrong-dim vectors to ``chunk_embeddings``.
    batch_size:
        Forwarded to the underlying ``encode`` call. Defaults to 32.
    device:
        ``"cpu"`` / ``"cuda"`` / ``"mps"`` / ``None``. ``None`` falls
        back to ``$KILN_EMBEDDING_DEVICE``, then to ``"cpu"``.
    model_factory:
        Test-only injection point. Production callers omit this.
    normalize:
        L2-normalize the output. Nomic Embed v1.5 documents
        normalize-by-default behavior; left configurable for models
        that prefer raw vectors.
    trust_remote_code:
        Forwarded to ``SentenceTransformer``. Required by models that
        ship custom modeling code (e.g. ``nomic-embed-text-v1.5`` ->
        ``nomic-bert-2048``); harmless-but-unnecessary for plain
        sentence-transformers models. Defaults to ``False`` so running
        code downloaded from a model hub is always an explicit,
        per-model opt-in via ``config/models.yaml`` — never a silent
        default. Keeping it in config (not hardcoded) is what lets the
        active model be swapped without touching this adapter.
    """

    provider = PROVIDER_NAME

    def __init__(
        self,
        model_name: str,
        dimensions: int,
        batch_size: int = DEFAULT_BATCH_SIZE,
        device: str | None = None,
        *,
        model_factory: ModelFactory | None = None,
        normalize: bool = True,
        trust_remote_code: bool = False,
    ) -> None:
        if dimensions <= 0:
            raise ValueError(f"dimensions must be positive, got {dimensions}")
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        self.model = model_name
        self.dimensions = dimensions
        self.batch_size = batch_size
        # device: explicit > env var > "cpu". Empty env strings are
        # ignored so an unset-but-present-in-environ variable doesn't
        # mask the cpu default.
        self.device = device or os.environ.get(DEVICE_ENV_VAR) or DEFAULT_DEVICE
        self.trust_remote_code = trust_remote_code
        self._factory = model_factory or _default_factory
        self._normalize = normalize
        self._encoder: Any | None = None
        self._load_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Alias for :attr:`model`. The task spec asked for this name."""
        return self.model

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
                logger.info(
                    "loading local embedding model: %s (device=%s)",
                    self.model,
                    self.device,
                )
                # Loading is CPU/GPU-heavy; keep the loop free.
                self._encoder = await asyncio.to_thread(
                    self._factory,
                    self.model,
                    device=self.device,
                    trust_remote_code=self.trust_remote_code,
                )
        return self._encoder

    def _encode_sync(self, encoder: Any, texts: list[str]) -> list[list[float]]:
        result = encoder.encode(
            texts,
            normalize_embeddings=self._normalize,
            convert_to_numpy=False,
            batch_size=self.batch_size,
        )
        # SentenceTransformer can return numpy arrays or torch tensors;
        # coerce to plain python lists for downstream uniformity.
        return [list(map(float, v)) for v in result]


# Symbol-level alias only. The Phase 4 ``LocalEmbeddingProvider`` was
# constructed with keyword-only ``LocalEmbeddingProvider(*, model=...,
# dimensions=...)``; the new canonical class takes positional
# ``LocalSentenceTransformersProvider(model_name, dimensions, ...)`` —
# the first arg was renamed and is no longer keyword-only.
# **External callers using the old ``model=`` keyword will break at
# runtime with TypeError.** All in-repo callers were updated in this
# PR; there is no in-tree deprecation cycle. Removing the alias is a
# future cleanup once any downstream consumers have migrated.
LocalEmbeddingProvider = LocalSentenceTransformersProvider


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_DEVICE",
    "DEVICE_ENV_VAR",
    "PROVIDER_NAME",
    "LocalEmbeddingProvider",
    "LocalSentenceTransformersProvider",
]
