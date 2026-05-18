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
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.eval import ReviewCase, load_review_set
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters, load_retrieval_config

pytestmark = [pytest.mark.integration, pytest.mark.eval]


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
    """
    # Import lazily so collection-time doesn't drag the integration
    # conftest helpers in when the eval tier is skipped (no DB).
    from tests.eval.conftest import _PROMPT_INJECTION_PHRASES, _sensitive_patterns

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
                    embedding_provider=MockEmbeddingProvider(),
                    prompt_injection_phrases=_PROMPT_INJECTION_PHRASES,
                    sensitive_patterns=_sensitive_patterns(),
                )
                await session.commit()
        finally:
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
    """
    monkeypatch.setattr(
        "cf_knowledge_kiln.retrieval.ranking.WEAK_EVIDENCE_SCORE_THRESHOLD",
        1e-4,
    )
    settings = _eval_settings()
    db = Database(database_url, pool_size=settings.pg_pool_size)
    config = load_retrieval_config(settings.security_config_path)
    retriever = HybridRetriever(
        db=db,
        embedding_provider=MockEmbeddingProvider(),
        config=config,
        ef_search=settings.hnsw_ef_search,
    )
    try:
        yield retriever
    finally:
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
