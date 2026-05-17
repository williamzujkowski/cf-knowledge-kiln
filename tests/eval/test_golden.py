"""Golden-set retrieval eval (#31).

Two layers of assertion:

1. **Per-case parametrized tests** — each expected hit must appear in
   the top ``must_appear_within_k`` results. Precise failure locality:
   pytest reports which case+hit slipped.

2. **Aggregate-threshold test** — mean MRR and recall@K across the
   whole set must beat the bootstrap floor. Catches the case where
   each individual case still squeaks past its per-hit cap but the
   distribution has shifted.

Bootstrap thresholds are deliberately loose; they should be ratcheted
up in follow-up PRs once a real embedding provider runs against the
set. The harness here is the substrate, not the bar.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cf_knowledge_kiln.eval import (
    EvalReport,
    load_golden_set,
    run_eval,
    write_json,
    write_markdown,
)
from cf_knowledge_kiln.eval.scoring import first_matching_rank
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters

pytestmark = [pytest.mark.integration, pytest.mark.eval]


# Bootstrap thresholds. Under MockEmbeddingProvider the vector arm is
# degenerate, so these measure FTS + RRF stability. Raise them as the
# golden set grows or once a real embedding provider runs.
BOOTSTRAP_MRR_FLOOR = 0.3
BOOTSTRAP_RECALL_AT_10_FLOOR = 0.6

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_PATH = _REPO_ROOT / "tests" / "eval" / "golden" / "docs.yaml"
_REPORTS_DIR = _REPO_ROOT / "tests" / "eval" / "reports"


def _load_cases() -> list:
    return load_golden_set(_GOLDEN_PATH)


@pytest.fixture
def eval_report(seeded_retriever: HybridRetriever) -> EvalReport:
    """Run the eval once and persist the report to disk for operators.

    Per-session caching is awkward when the retriever fixture is
    function-scoped (the integration tier's ``database_url`` is the
    only session-scoped seam we can lean on). The eval is small —
    rerunning it for the aggregate test is fine.
    """

    async def _go() -> EvalReport:
        cases = _load_cases()
        return await run_eval(seeded_retriever, cases)

    report = asyncio.run(_go())
    # Always persist the latest run so PR authors can inspect/include it.
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    write_json(report, _REPORTS_DIR / f"{ts}.json")
    write_markdown(report, _REPORTS_DIR / f"{ts}.md")
    write_json(report, _REPORTS_DIR / "latest.json")
    write_markdown(report, _REPORTS_DIR / "latest.md")
    return report


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c.case_id)
def test_case_meets_must_appear_within_k(case, seeded_retriever: HybridRetriever) -> None:
    """Each expected hit must rank inside its declared must_appear_within_k."""

    async def _search():
        return await seeded_retriever.search(
            case.query,
            filters=RetrievalFilters(**case.filters),
            max_results=10,
        )

    result = asyncio.run(_search())
    misses: list[str] = []
    for hit in case.expected:
        rank = first_matching_rank(hit, result.chunks, result.document_refs)
        if rank is None or rank >= hit.must_appear_within_k:
            misses.append(
                f"{hit.path} § {'/'.join(hit.heading_path) or '(doc)'} "
                f"→ rank={rank}, must_appear_within_k={hit.must_appear_within_k}"
            )
    assert not misses, "\n".join(misses)


def test_aggregate_thresholds(eval_report: EvalReport) -> None:
    """Mean MRR + recall@10 across the golden set must beat the floor."""
    agg = eval_report.aggregate
    assert agg.mrr >= BOOTSTRAP_MRR_FLOOR, (
        f"MRR {agg.mrr:.3f} below bootstrap floor {BOOTSTRAP_MRR_FLOOR:.3f}"
    )
    assert agg.recall_at[10] >= BOOTSTRAP_RECALL_AT_10_FLOOR, (
        f"Recall@10 {agg.recall_at[10]:.3f} below floor {BOOTSTRAP_RECALL_AT_10_FLOOR:.3f}"
    )
