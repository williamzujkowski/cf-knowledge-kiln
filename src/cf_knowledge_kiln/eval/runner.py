"""Drive :class:`HybridRetriever` over a golden set and collect scores.

Async because the retriever is async. The pytest suite and the
``__main__`` CLI both call into ``run_eval`` — single source of truth
for how the engine is exercised.
"""

from __future__ import annotations

from cf_knowledge_kiln.eval.dataset import GoldenCase
from cf_knowledge_kiln.eval.scoring import (
    AggregateMetrics,
    CaseResult,
    EvalReport,
    aggregate,
    score_one,
)
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters

DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)


async def run_eval(
    retriever: HybridRetriever,
    cases: list[GoldenCase],
    *,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
) -> EvalReport:
    """Run every case through the retriever and return per-case + aggregate scores.

    The retriever uses ``max(k_values)`` as ``max_results`` so the
    largest-K recall measurement is well-defined. Filter values from
    each case are passed through :class:`RetrievalFilters` validation —
    bad filters surface as a Pydantic error attributed to the case_id.
    """
    if not k_values:
        raise ValueError("k_values must be non-empty")
    if any(k <= 0 for k in k_values):
        raise ValueError(f"k_values must all be positive, got {k_values}")
    per_case: list[CaseResult] = []
    top_k = max(k_values)
    for case in cases:
        result = await retriever.search(
            case.query,
            filters=RetrievalFilters(**case.filters),
            max_results=top_k,
        )
        per_case.append(score_one(case, result.chunks, result.document_refs, k_values))
    return EvalReport(
        per_case=per_case,
        aggregate=aggregate(per_case, k_values),
        k_values=k_values,
    )


__all__ = ["DEFAULT_K_VALUES", "AggregateMetrics", "EvalReport", "run_eval"]
