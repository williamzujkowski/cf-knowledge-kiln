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
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Canonical provider name. ``local`` is preserved as a back-compat alias
# in the factory's registry — both resolve to this class.
PROVIDER_NAME = "local-sentence-transformers"

DEFAULT_BATCH_SIZE = 32
DEFAULT_DEVICE = "cpu"
DEVICE_ENV_VAR = "KILN_EMBEDDING_DEVICE"


# #204: model-family text prefixes. Some embedding models were trained
# with explicit role prefixes (e5 uses ``passage: `` / ``query: ``;
# Nomic Embed uses ``search_document: `` / ``search_query: ``). Calling
# ``encode([raw_text])`` on these models without the prefix collapses
# cosine similarities into the 0.1-0.4 band — the user-reported max
# top-1 score of 0.300 on e5-small-v2 is exactly this signature.
# Patterns are matched against the model name in lookup order; first
# match wins. Models not matching any pattern get no prefix (cohere,
# bge-m3, snowflake-arctic, etc. don't require one).
#
# Add new families here, not at the call site. Pattern syntax is
# anchored to start-of-string but not end-of-string so suffix variants
# (e.g. ``-v1.5``, ``-instruct``) ride along without a per-version
# entry.
_MODEL_PREFIXES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # e5 family — passage / query.
    # Covers: intfloat/e5-small-v2, e5-base-v2, e5-large-v2,
    # multilingual-e5-*, e5-mistral-7b-instruct.
    (re.compile(r"^intfloat/(multilingual-)?e5-"), "passage: ", "query: "),
    # Nomic Embed v1 family — search_document / search_query.
    # Covers: nomic-ai/nomic-embed-text-v1, -v1.5.
    (
        re.compile(r"^nomic-ai/nomic-embed-text-v1"),
        "search_document: ",
        "search_query: ",
    ),
)


def _prefixes_for(model_name: str) -> tuple[str, str]:
    """Return ``(passage_prefix, query_prefix)`` for ``model_name``.

    Empty strings for both when no entry in :data:`_MODEL_PREFIXES`
    matches — the model doesn't need prefixes (or kiln doesn't yet
    know that it does; add it to the table). #204.
    """
    for pattern, passage, query in _MODEL_PREFIXES:
        if pattern.match(model_name):
            return passage, query
    return "", ""


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
        """Raw encode, no model-family prefix.

        Backward-compat path for callers that genuinely don't have a
        passage/query distinction (the startup health probe in
        ``api/app.py``). New code should prefer the explicit
        :meth:`embed_documents` / :meth:`embed_query` methods (#204).
        """
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

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` as passages/documents, applying the model-family prefix.

        #204: e5 ``passage: ``, Nomic ``search_document: ``. Without the
        prefix, cosine similarities on these models collapse into the
        0.1-0.4 band — the user's golden-set max was 0.300 instead of
        the calibrated >0.46.
        """
        if not texts:
            return []
        passage_prefix, _ = _prefixes_for(self.model)
        prefixed = [f"{passage_prefix}{t}" for t in texts] if passage_prefix else texts
        return await self.embed(prefixed)

    async def embed_query(self, text: str) -> list[float]:
        """Embed ``text`` as a single query, applying the model-family prefix.

        Returns the vector directly (not a list-of-one) to match the
        Protocol shape — query callers always want exactly one vector.
        """
        _, query_prefix = _prefixes_for(self.model)
        prefixed = f"{query_prefix}{text}" if query_prefix else text
        return (await self.embed([prefixed]))[0]

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
