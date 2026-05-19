"""Hand-labeled ``requires_human_review`` precision eval (#108 item 1).

Loads ``tests/eval/golden/review_precision.yaml`` and asserts that the
retriever's context-pack ``requires_human_review`` boolean matches the
hand-label for each case. Aggregate precision must beat
:data:`REVIEW_PRECISION_FLOOR` (≥ 0.85 on the bootstrap 12-case set);
ratchet the floor up as the labeled set grows.

The corpus is ``docs/_eval/``: a small kiln-self-referential tree
deliberately authored to exercise every branch of
:func:`cf_knowledge_kiln.retrieval.ranking.requires_human_review`
(conflicting sources, deprecated-only result set, sensitive content,
prompt-injection pattern, weak evidence, empty result, plus six clean
controls). The pre-existing ``docs/`` corpus stays clean — these
fixtures live in their own subtree so production ingest can opt out
via the ``exclude: - 'docs/_eval/**'`` rule.

Two embedding modes are supported (#108 item 2):

* Default (``MockEmbeddingProvider``) — the vector arm is degenerate;
  exercises the FTS + RRF + decision-branch logic only.
* ``KILN_EVAL_REAL_EMBEDDINGS=1`` — swaps in the local
  sentence-transformers provider (Nomic Embed v1.5, 768d). The
  per-bucket confidence-calibration test runs only in this mode and
  consumes the ``relevance`` grades on each :class:`ReviewCase`.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.eval import ReviewCase, load_review_set
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider, MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters, load_retrieval_config
from cf_knowledge_kiln.retrieval.types import ContextPackResponse

pytestmark = [pytest.mark.integration, pytest.mark.eval]


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
_PER_BUCKET_PRECISION_FLOOR = 0.9


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
    try:
        # PR A lands ``LocalSentenceTransformersProvider`` here.
        from cf_knowledge_kiln.ingestion.embedding import (  # type: ignore[attr-defined]
            LocalSentenceTransformersProvider,
        )
    except ImportError as exc:
        pytest.skip(
            f"{_REAL_EMBEDDINGS_ENV}=1 requested but "
            f"LocalSentenceTransformersProvider unavailable: {exc}. "
            "Install with: pip install 'cf-knowledge-kiln[embeddings]' "
            "(and ensure PR A has landed)"
        )
    device = os.environ.get(_EMBEDDING_DEVICE_ENV, "cpu")
    return LocalSentenceTransformersProvider(
        model=_REAL_EMBEDDING_MODEL,
        dimensions=_REAL_EMBEDDING_DIMENSIONS,
        device=device,
    )


REVIEW_PRECISION_FLOOR = 0.83
"""Precision threshold on the 12-case labeled set.

Measured baseline under :class:`MockEmbeddingProvider` after the
PR #145 review fixes (auth-policy heading-collision narrowed to the
intended ``Bearer token rotation policy`` heading; one clean-case
query tightened off keywords the injection-trap chunk competed for):
**12/12 = 1.000**.

The floor is set at 0.83 (≤ 2 failures on 12) so:

* the gate still trips on a real regression that flips two cases
* mock-noise drift between runs (the vector arm is degenerate, FTS
  ranks shift slightly when the corpus is reingested) doesn't
  false-fail on the rare flake
* the issue's stated long-run target (≥ 0.95) lands once #108 item 2
  swaps in a real embedding provider; at that point grow the corpus
  to ~30 cases and ratchet to 0.90 → 0.95.
"""


_REPO_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_SET = _REPO_ROOT / "tests" / "eval" / "golden" / "review_precision.yaml"
_EVAL_CORPUS_DIR = _REPO_ROOT / "docs" / "_eval"


# Reuse the same prompt-injection + sensitive-content patterns the
# journey suite uses (see tests/eval/conftest.py). Lazy-imported via
# the module path to avoid duplicating the regex list here — drift
# between the eval tiers would mask a real scanner-config bug.
def _eval_settings() -> Settings:
    return Settings(
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=500,
        ingest_max_repo_bytes=10 * 1_048_576,
    )


@pytest.fixture
def review_corpus_seeded(database_url: str) -> Iterator[None]:
    """Ingest docs/_eval/ with PI + sensitive scanners armed.

    Function-scoped because the autouse truncate fixture wipes the DB
    before each test. The corpus is small (~18 files, ~50 chunks)
    so the cost is acceptable.

    Embedding provider is chosen per :func:`_build_embedding_provider`
    (mock by default, Nomic Embed v1.5 under
    ``KILN_EVAL_REAL_EMBEDDINGS=1``). The ingest-time and query-time
    providers MUST match — embeddings written by mock cannot be
    queried by Nomic and vice-versa, so this fixture builds the same
    provider the retriever fixture uses.
    """
    # Import lazily so collection-time doesn't drag the integration
    # conftest helpers in when the eval tier is skipped (no DB).
    from tests.eval.conftest import _PROMPT_INJECTION_PHRASES, _sensitive_patterns

    provider = _build_embedding_provider()

    async def _seed() -> None:
        eng: AsyncEngine = create_async_engine(database_url)
        try:
            maker = async_sessionmaker(eng, expire_on_commit=False)
            async with maker() as session:
                await run_source(
                    session,
                    source=LocalSource(
                        name="kiln-eval",
                        type="local",
                        path=str(_EVAL_CORPUS_DIR),
                        include=["*.md"],
                    ),
                    settings=_eval_settings(),
                    embedding_provider=provider,
                    prompt_injection_phrases=_PROMPT_INJECTION_PHRASES,
                    sensitive_patterns=_sensitive_patterns(),
                )
                await session.commit()
        finally:
            await provider.aclose()
            await eng.dispose()

    asyncio.run(_seed())
    yield


@pytest.fixture
def review_retriever(
    review_corpus_seeded: None,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[HybridRetriever]:
    """Build a retriever for the precision eval, with one calibration tweak.

    Under :class:`MockEmbeddingProvider` the vector arm is degenerate
    and fused RRF scores top out around 0.025 — well below the
    production ``WEAK_EVIDENCE_SCORE_THRESHOLD = 0.5``. That would
    trip the weak-evidence short-circuit on every case and collapse
    the precision signal to chance.

    For this tier we patch the threshold down to a near-zero floor so
    the weak-evidence path fires only on the deliberately-distant
    ``weak-novel-topic`` case (where it's still meaningful relative
    to the other cases). Item 2 of #108 — confidence calibration on
    real embeddings — is the proper fix and re-baselines the
    threshold; this eval focuses on the non-vector decision branches
    (conflicting / deprecated / sensitive / injection / empty).

    Under ``KILN_EVAL_REAL_EMBEDDINGS=1`` the threshold patch is
    SKIPPED — production thresholds apply to real-embedding scores.
    """
    if not _real_embeddings_requested():
        monkeypatch.setattr(
            "cf_knowledge_kiln.retrieval.ranking.WEAK_EVIDENCE_SCORE_THRESHOLD",
            1e-4,
        )
    settings = _eval_settings()
    db = Database(database_url, pool_size=settings.pg_pool_size)
    config = load_retrieval_config(settings.security_config_path)
    provider = _build_embedding_provider()
    retriever = HybridRetriever(
        db=db,
        embedding_provider=provider,
        config=config,
        ef_search=settings.hnsw_ef_search,
    )
    try:
        yield retriever
    finally:
        asyncio.run(provider.aclose())
        asyncio.run(db.dispose())


@pytest.fixture(scope="session")
def review_cases() -> list[ReviewCase]:
    return load_review_set(_REVIEW_SET)


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


def test_review_set_loads(review_cases: list[ReviewCase]) -> None:
    """Schema sanity — the YAML parses, has both polarities, no dupes."""
    assert len(review_cases) >= 10, "bootstrap set must have ≥ 10 cases"
    review_true = [c for c in review_cases if c.expected_review]
    review_false = [c for c in review_cases if not c.expected_review]
    assert review_true, "need at least one expected-review-required case"
    assert review_false, "need at least one expected-no-review case"
    # Loader already enforces case_id uniqueness; this is a belt + braces.
    ids = [c.case_id for c in review_cases]
    assert len(ids) == len(set(ids))


def test_review_decisions_meet_precision_floor(
    review_retriever: HybridRetriever,
    review_cases: list[ReviewCase],
) -> None:
    """Drive every case in one event loop, report per-case + aggregate.

    A parametrized per-case test would spawn a fresh ``asyncio.run``
    event loop per case and the second one would inherit asyncpg
    connections from the first — asyncpg's "got Future attached to
    a different loop" error. The journey suite handles this the same
    way (one sweep per test, asserting both the per-case detail and
    the aggregate result).

    Per-case mismatches are collected and reported in the assertion
    message so a failure still points at the responsible cases; the
    aggregate floor catches the distribution-shift case where each
    individual mismatch is recoverable but the precision crater is
    real.
    """

    async def _sweep() -> list[tuple[ReviewCase, bool]]:
        return [(c, await _run_one(review_retriever, c)) for c in review_cases]

    results = asyncio.run(_sweep())
    mismatches = [(c, actual) for c, actual in results if actual != c.expected_review]
    correct = len(results) - len(mismatches)
    precision = correct / len(results)

    report_lines = [f"review-decision precision: {correct}/{len(results)} = {precision:.3f}"]
    if mismatches:
        report_lines.append("Mismatches:")
        for case, actual in mismatches:
            report_lines.append(
                f"  - {case.case_id:30s} expected={case.expected_review} "
                f"got={actual} (reason: {case.expected_reason})"
            )
            report_lines.append(f"      query: {case.query!r}")
    report = "\n".join(report_lines)

    assert precision >= REVIEW_PRECISION_FLOOR, f"{report}\nfloor: {REVIEW_PRECISION_FLOOR:.2f}"


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


def test_confidence_buckets_meet_per_bucket_precision(
    review_retriever: HybridRetriever,
    review_cases: list[ReviewCase],
) -> None:
    """Per-bucket precision on real embeddings (#108 item 2).

    Skips unless ``KILN_EVAL_REAL_EMBEDDINGS=1`` — the calibration
    signal requires a real vector arm. Under mock the bucket
    distribution is degenerate (every score collapses to ``low``)
    so the gate would pass trivially without exercising the
    confidence ladder.

    Drives all 12 cases through ``context_pack``, groups results by
    ``pack.confidence``, and asserts each populated bucket meets
    :data:`_PER_BUCKET_PRECISION_FLOOR` (≥ 0.9). "Correct" is
    defined by :func:`_bucket_correct`: tripped-review for positive
    cases, or top-1 chunk graded ≥ 2 for negative cases.

    Ungraded results (top-1 not in the strawman map) count against
    the bucket — they are surfaced in the assertion message so
    successive spot-check passes can extend the grade map without
    re-running the suite blindly.
    """
    if not _real_embeddings_requested():
        pytest.skip(
            f"set {_REAL_EMBEDDINGS_ENV}=1 to run confidence-bucket calibration "
            "(requires the 'embeddings' extra and a real embedding provider)"
        )

    async def _sweep() -> list[tuple[ReviewCase, ContextPackResponse]]:
        return [(c, await _run_one_pack(review_retriever, c)) for c in review_cases]

    results = asyncio.run(_sweep())
    buckets: dict[str, list[tuple[ReviewCase, ContextPackResponse, bool]]] = defaultdict(list)
    for case, pack in results:
        correct = _bucket_correct(case, pack)
        buckets[pack.confidence or "none"].append((case, pack, correct))

    report_lines = ["per-bucket calibration:"]
    failures: list[str] = []
    for bucket in ("high", "medium", "low", "none"):
        entries = buckets.get(bucket, [])
        if not entries:
            report_lines.append(f"  {bucket:6s}: (empty)")
            continue
        correct = sum(1 for _, _, c in entries if c)
        precision = correct / len(entries)
        report_lines.append(f"  {bucket:6s}: {correct}/{len(entries)} = {precision:.3f}")
        for case, pack, ok in entries:
            top_key = _citation_key(pack.evidence[0]) if pack.evidence else "(no evidence)"
            marker = "ok " if ok else "FAIL"
            report_lines.append(f"      [{marker}] {case.case_id:30s} top1={top_key!r}")
        if precision < _PER_BUCKET_PRECISION_FLOOR:
            failures.append(
                f"bucket {bucket!r} precision {precision:.3f} < floor {_PER_BUCKET_PRECISION_FLOOR}"
            )

    report = "\n".join(report_lines)
    assert not failures, f"{report}\n" + "\n".join(failures)
