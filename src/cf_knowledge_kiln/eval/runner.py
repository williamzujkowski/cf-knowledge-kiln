"""Drive :class:`HybridRetriever` over a golden set and collect scores.

Async because the retriever is async. The pytest suite and the
``__main__`` CLI both call into ``run_eval`` — single source of truth
for how the engine is exercised.
"""

from __future__ import annotations

from typing import Any

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
    bad filters fail loudly rather than silently degrading the score.
    """
    per_case: list[CaseResult] = []
    top_k = max(k_values)
    for case in cases:
        filters = _build_filters(case.filters)
        result = await retriever.search(
            case.query,
            filters=filters,
            max_results=top_k,
        )
        per_case.append(score_one(case, result.chunks, result.document_refs, k_values))
    return EvalReport(
        per_case=per_case,
        aggregate=aggregate(per_case, k_values),
        k_values=k_values,
    )


def _build_filters(raw: dict[str, Any]) -> RetrievalFilters:
    """Best-effort coercion of golden-set filter dicts to :class:`RetrievalFilters`.

    Authors write ``status: [active]`` in YAML; the model field is
    ``status: list[str] | None``. Pydantic v2 handles the conversion
    when we hand the raw dict in, so this is mostly a passthrough — we
    keep it as a seam in case the filter shape grows.
    """
    return RetrievalFilters(**raw)


__all__ = ["DEFAULT_K_VALUES", "AggregateMetrics", "EvalReport", "run_eval"]
