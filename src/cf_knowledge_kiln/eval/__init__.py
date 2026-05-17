"""Retrieval evaluation harness (Phase 9, issue #31).

Public surface:

* :func:`load_golden_set` — parse a golden YAML.
* :func:`run_eval` — drive a :class:`HybridRetriever` and score cases.
* :func:`to_markdown` / :func:`write_json` — emit reports.

Internal modules group concerns:

* ``dataset`` — YAML schema + ``GoldenCase`` / ``ExpectedHit`` dataclasses.
* ``scoring`` — pure recall@K + MRR functions and aggregation.
* ``runner`` — async driver over :class:`HybridRetriever`.
* ``report`` — JSON + Markdown serialization.
"""

from cf_knowledge_kiln.eval.dataset import (
    ExpectedHit,
    GoldenCase,
    GoldenSetError,
    load_golden_set,
)
from cf_knowledge_kiln.eval.journey_scoring import (
    LatencyMetrics,
    citation_presence_rate,
    latency_metrics,
    sensitive_chunks_excluded,
    token_budget_respected,
    untrusted_notice_present,
    warning_emitted,
    warning_kinds_in,
)
from cf_knowledge_kiln.eval.report import (
    to_json_dict,
    to_markdown,
    write_json,
    write_markdown,
)
from cf_knowledge_kiln.eval.runner import DEFAULT_K_VALUES, run_eval
from cf_knowledge_kiln.eval.scoring import (
    AggregateMetrics,
    CaseResult,
    EvalReport,
    aggregate,
    first_matching_rank,
    matches,
    recall_at_k,
    reciprocal_rank,
    score_one,
)

__all__ = [
    "DEFAULT_K_VALUES",
    "AggregateMetrics",
    "CaseResult",
    "EvalReport",
    "ExpectedHit",
    "GoldenCase",
    "GoldenSetError",
    "LatencyMetrics",
    "aggregate",
    "citation_presence_rate",
    "first_matching_rank",
    "latency_metrics",
    "load_golden_set",
    "matches",
    "recall_at_k",
    "reciprocal_rank",
    "run_eval",
    "score_one",
    "sensitive_chunks_excluded",
    "to_json_dict",
    "to_markdown",
    "token_budget_respected",
    "untrusted_notice_present",
    "warning_emitted",
    "warning_kinds_in",
    "write_json",
    "write_markdown",
]
