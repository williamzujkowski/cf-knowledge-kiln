"""Integration smoke test for the local sentence-transformers provider.

Skipped unless ``KILN_TEST_REAL_EMBEDDINGS=1`` is set in the
environment. When enabled, this test downloads the default Nomic
Embed v1.5 weights (~500 MB) from HuggingFace into ``~/.cache/huggingface/``
and runs a real ``encode`` pass. Useful for verifying:

* The full dependency chain (sentence-transformers → torch → numpy
  → HF hub) resolves on the host.
* The default model produces 768-dim vectors as documented.
* Vectors come back L2-normalized — Nomic's documented behavior, which
  hybrid retrieval's cosine-distance math depends on.

Not part of regular CI; runs locally and in opt-in workflows that
pre-cache the model.
"""

from __future__ import annotations

import math
import os

import pytest

from cf_knowledge_kiln.ingestion.embedding.local import (
    LocalSentenceTransformersProvider,
)

pytestmark = pytest.mark.integration

REAL_EMBEDDINGS_ENV = "KILN_TEST_REAL_EMBEDDINGS"
NOMIC_MODEL = "nomic-ai/nomic-embed-text-v1.5"
NOMIC_DIM = 768


def _opted_in() -> bool:
    return os.environ.get(REAL_EMBEDDINGS_ENV) == "1"


@pytest.mark.skipif(
    not _opted_in(),
    reason=(
        f"set {REAL_EMBEDDINGS_ENV}=1 to run the real local-inference "
        "smoke test (downloads ~500 MB of model weights on first use)"
    ),
)
async def test_nomic_embed_v1_5_produces_unit_vectors_at_declared_dim() -> None:
    """Smoke-test: real Nomic Embed v1.5 encodes two strings correctly."""
    # Importing here so the module-collect step doesn't blow up when
    # the optional `real-embeddings` extra isn't installed.
    pytest.importorskip(
        "sentence_transformers",
        reason="install with: pip install -e '.[real-embeddings]'",
    )

    provider = LocalSentenceTransformersProvider(
        model_name=NOMIC_MODEL,
        dimensions=NOMIC_DIM,
        device="cpu",
    )
    try:
        vectors = await provider.embed(
            [
                "cloud foundry is a platform-as-a-service",
                "postgres pgvector enables similarity search",
            ]
        )
    finally:
        await provider.aclose()

    assert len(vectors) == 2
    for v in vectors:
        assert len(v) == NOMIC_DIM
        norm = math.sqrt(sum(x * x for x in v))
        # Nomic documents normalize-by-default; we pass
        # ``normalize_embeddings=True`` so vectors must be unit-length
        # within float tolerance. A noticeable deviation here means
        # either the model changed behavior or the provider stopped
        # passing the normalize flag.
        assert abs(norm - 1.0) < 1e-3, f"expected unit-norm vector, got |v|={norm}"
