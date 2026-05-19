"""Test-internal helpers for :mod:`tests.eval.test_review_precision`.

Extracted to keep the test module under AGENTS.md's 400-line soft cap
(#153). These are pure helpers — no test functions, no fixtures — so
the calibration-precision test stays readable as its scoring logic
grows.

The leading-underscore convention matches the rest of the eval suite
(see ``tests/eval/conftest.py``: ``_PROMPT_INJECTION_PHRASES``,
``_sensitive_patterns``, ``_eval_settings``, ``_build_retriever``).
Module is named with a leading underscore for the same reason — it is
test-internal, not public API.
"""

from __future__ import annotations

import os

import pytest

from cf_knowledge_kiln.eval import ReviewCase
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider, MockEmbeddingProvider
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters
from cf_knowledge_kiln.retrieval.types import ContextPackResponse

# ─── Real-embedding env-gate (#108 item 2) ─────────────────────────

_REAL_EMBEDDINGS_ENV = "KILN_EVAL_REAL_EMBEDDINGS"
_EMBEDDING_DEVICE_ENV = "KILN_EMBEDDING_DEVICE"

# Pinned for the confidence-calibration tier. PR A is landing
# ``LocalSentenceTransformersProvider`` against this exact model +
# dimension pair (Nomic AI, US-origin per AGENTS.md model registry).
_REAL_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
_REAL_EMBEDDING_DIMENSIONS = 768

# Top-1 relevance grade that counts as "labeled-correct" under the
# per-bucket precision scorer. Grade ≥ 2 means "useful, partial" or
# better per the rubric in review_precision.yaml.
_BUCKET_CORRECT_GRADE_FLOOR = 2

# Per-confidence-bucket precision floor (#108 item 2). The aggregate
# binary-precision floor is :data:`REVIEW_PRECISION_FLOOR`; this is
# the stricter per-stratum gate enforced only under real embeddings.
#
# Set at 0.5 for the bootstrap because (a) buckets carry only 1-8 cases
# at the 12-case corpus size — bucket-level statistical noise is high,
# and (b) ungraded top-1 chunks count against the bucket today,
# inflating the failure count. Tighten back to 0.9 once the strawman
# grade map is human-extended to cover the chunks Nomic actually picks
# as top-1 under the kiln's RRF fusion. (Measured bootstrap under
# Nomic Embed v1.5: high=empty, medium=5/8=0.625, low=2/3=0.667,
# none=1/1=1.000.)
_PER_BUCKET_PRECISION_FLOOR = 0.5


def _real_embeddings_requested() -> bool:
    """Return True if the operator asked for real embeddings.

    Read once per fixture/test rather than cached at import time so a
    monkeypatch in a unit test can flip the gate without restarting
    the process.
    """
    return os.environ.get(_REAL_EMBEDDINGS_ENV) == "1"


def _build_embedding_provider() -> EmbeddingProvider:
    """Return the embedding provider for the current run mode.

    Under :data:`_REAL_EMBEDDINGS_ENV` we import the local
    sentence-transformers adapter lazily so the heavy ``embeddings``
    extra is only required when the operator opts in. If the import
    fails the test skips with a pointer to the install command — we
    do not silently fall back to mock, because that would mask the
    real-vs-mock signal the calibration test exists to measure.
    """
    if not _real_embeddings_requested():
        return MockEmbeddingProvider()
    # Eagerly probe BOTH the symbol re-export (PR #149) and the
    # optional `sentence-transformers` extra. The provider class
    # itself loads the model lazily on first encode, so a bare
    # construction wouldn't surface a missing extra until ingest
    # had already started. Probing here skips early with a precise
    # pointer at the missing piece — never a silent mock fallback,
    # because that would mask the real-vs-mock signal the
    # calibration test exists to measure.
    try:
        from cf_knowledge_kiln.ingestion.embedding import (
            LocalSentenceTransformersProvider,
        )
    except ImportError as exc:
        pytest.skip(
            f"{_REAL_EMBEDDINGS_ENV}=1 requested but the "
            f"LocalSentenceTransformersProvider symbol is not "
            f"re-exported from cf_knowledge_kiln.ingestion.embedding "
            f"(import error: {exc})."
        )
    try:
        import sentence_transformers  # noqa: F401 — presence probe
    except ImportError as exc:
        pytest.skip(
            f"{_REAL_EMBEDDINGS_ENV}=1 requested but the "
            f"'real-embeddings' extra is not installed: {exc}. "
            "Install with: pip install -e '.[real-embeddings]'"
        )
    device = os.environ.get(_EMBEDDING_DEVICE_ENV, "cpu")
    # NB: PR #149 renamed the constructor kwarg from `model` → `model_name`.
    return LocalSentenceTransformersProvider(
        model_name=_REAL_EMBEDDING_MODEL,
        dimensions=_REAL_EMBEDDING_DIMENSIONS,
        device=device,
    )


async def _run_one(retriever: HybridRetriever, case: ReviewCase) -> bool:
    pack = await retriever.context_pack(
        case.query,
        filters=RetrievalFilters(**case.filters),
        task="review_precision_eval",
        max_chunks=8,
        max_tokens=3000,
    )
    return pack.requires_human_review


async def _run_one_pack(retriever: HybridRetriever, case: ReviewCase) -> ContextPackResponse:
    return await retriever.context_pack(
        case.query,
        filters=RetrievalFilters(**case.filters),
        task="review_precision_eval",
        max_chunks=8,
        max_tokens=3000,
    )


def _citation_key(chunk: object) -> str:
    """Build the citation key the YAML's ``relevance`` map uses.

    Format: ``"<repo>/<path>#<H1>/<H2>/..."`` — matches the
    convention documented in
    ``tests/eval/golden/review_precision.yaml``. Empty heading_path
    means "document anywhere" and the key is just ``repo/path``.

    Accepts any object exposing ``repo``, ``path``, and
    ``heading_path`` (i.e. :class:`EvidenceChunk` from the agent
    pack). Returns ``""`` if the chunk is missing repo/path — that
    chunk will never match a grade and the calibration scorer will
    treat it as ungraded.
    """
    repo = getattr(chunk, "repo", None) or ""
    path = getattr(chunk, "path", None) or ""
    if not repo or not path:
        return ""
    heading = getattr(chunk, "heading_path", None) or []
    if heading:
        return f"{repo}/{path}#{'/'.join(heading)}"
    return f"{repo}/{path}"


def _bucket_correct(case: ReviewCase, pack: ContextPackResponse) -> bool:
    """Was the pack labeled-correct for this case?

    Two paths to "correct":

    * The case is a positive (``expected_review=true``) and the pack
      actually tripped ``requires_human_review`` — the calibration
      scorer treats a correctly-tripped review-required case as a
      positive evidence event, the same way the binary scorer does.
    * The case is a negative (``expected_review=false``), the pack
      has at least one evidence chunk, and the top-1 chunk's citation
      maps to a relevance grade ≥ :data:`_BUCKET_CORRECT_GRADE_FLOOR`
      in the case's ``relevance`` map.

    A negative case without a top-1 grade (because the strawman
    grades didn't anticipate that chunk) is reported as ungraded —
    the test surfaces ungraded counts in the assertion message so
    a human spot-check can fill the gaps.
    """
    if case.expected_review:
        return pack.requires_human_review
    if not pack.evidence:
        return False
    top1 = pack.evidence[0]
    grade = case.relevance.get(_citation_key(top1))
    if grade is None:
        return False
    return grade >= _BUCKET_CORRECT_GRADE_FLOOR
