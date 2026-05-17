"""Pure scoring functions for the retrieval eval harness (issue #31).

Recall@K and MRR over a ranked list of :class:`RankedChunk`. NDCG is
deferred — we only have binary relevance signal in the golden set,
which makes graded DCG noise. Add NDCG when we have a labelled
multi-relevance corpus.

All functions are pure and have no DB or network dependencies; the
runner module (``runner.py``) drives the retriever and feeds chunks
into these. Keeping scoring pure makes property tests cheap and lets
us audit threshold changes by replaying captured chunk lists offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from cf_knowledge_kiln.eval.dataset import ExpectedHit, GoldenCase
from cf_knowledge_kiln.retrieval.ranking import RankedChunk


def matches(hit: ExpectedHit, chunk: RankedChunk, refs: dict[UUID, Any]) -> bool:
    """Return True if ``chunk`` is the expected hit.

    Match by ``(repo, path)`` on the chunk's document and (when set)
    by exact heading_path equality. ``heading_path: []`` in the
    expected hit is the "doc-anywhere" wildcard — any chunk inside the
    document matches.
    """
    ref = refs.get(chunk.document_id)
    if ref is None:
        return False
    if getattr(ref, "repo", None) != hit.repo:
        return False
    if getattr(ref, "path", None) != hit.path:
        return False
    if not hit.heading_path:
        return True
    return list(chunk.heading_path) == hit.heading_path


def first_matching_rank(
    hit: ExpectedHit, chunks: list[RankedChunk], refs: dict[UUID, Any]
) -> int | None:
    """Return the 0-indexed rank of the first chunk matching ``hit``.

    ``None`` means the hit never appeared in ``chunks``. Callers
    converting to recall@K should treat ``None`` as "miss".
    """
    for rank, chunk in enumerate(chunks):
        if matches(hit, chunk, refs):
            return rank
    return None


def recall_at_k(
    case: GoldenCase,
    chunks: list[RankedChunk],
    refs: dict[UUID, Any],
    k: int,
) -> float:
    """Fraction of expected hits that appear in the top ``k`` results.

    Each ``ExpectedHit`` in ``case.expected`` is treated as a distinct
    relevant item; the score is the share of those items whose first
    matching rank is < ``k``.
    """
    if not case.expected or k <= 0:
        return 0.0
    found = 0
    for hit in case.expected:
        rank = first_matching_rank(hit, chunks, refs)
        if rank is not None and rank < k:
            found += 1
    return found / len(case.expected)


def reciprocal_rank(case: GoldenCase, chunks: list[RankedChunk], refs: dict[UUID, Any]) -> float:
    """Reciprocal rank of the FIRST expected hit found.

    Standard MRR semantics: 1.0 if the first relevant item is at rank
    0, 0.5 at rank 1, 0.333 at rank 2, … 0.0 if none appear.
    """
    if not case.expected:
        return 0.0
    best_rank: int | None = None
    for hit in case.expected:
        rank = first_matching_rank(hit, chunks, refs)
        if rank is not None and (best_rank is None or rank < best_rank):
            best_rank = rank
    if best_rank is None:
        return 0.0
    return 1.0 / (best_rank + 1)


# ─── Aggregation ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaseResult:
    """Per-case score breakdown.

    ``per_hit_ranks`` is one entry per ``case.expected``, in the
    same order: 0-indexed rank in the retrieved chunks list, or
    ``None`` if the expected hit was not in the top results.
    """

    case_id: str
    mrr: float
    recall_at: dict[int, float]
    per_hit_ranks: list[int | None] = field(default_factory=list)
    first_miss: ExpectedHit | None = None


@dataclass(frozen=True)
class AggregateMetrics:
    """Mean metrics across the golden set."""

    mrr: float
    recall_at: dict[int, float]
    case_count: int


@dataclass(frozen=True)
class EvalReport:
    """One full eval pass."""

    per_case: list[CaseResult]
    aggregate: AggregateMetrics
    k_values: tuple[int, ...] = field(default_factory=lambda: (1, 3, 5, 10))


def aggregate(per_case: list[CaseResult], k_values: tuple[int, ...]) -> AggregateMetrics:
    """Arithmetic-mean MRR and recall@K across all cases."""
    n = len(per_case)
    if n == 0:
        return AggregateMetrics(mrr=0.0, recall_at=dict.fromkeys(k_values, 0.0), case_count=0)
    mrr_sum = sum(r.mrr for r in per_case)
    recall_sums = {k: sum(r.recall_at.get(k, 0.0) for r in per_case) for k in k_values}
    return AggregateMetrics(
        mrr=mrr_sum / n,
        recall_at={k: recall_sums[k] / n for k in k_values},
        case_count=n,
    )


def score_one(
    case: GoldenCase,
    chunks: list[RankedChunk],
    refs: dict[UUID, Any],
    k_values: tuple[int, ...],
) -> CaseResult:
    """Score one case against its retrieved chunks + doc refs."""
    ranks = [first_matching_rank(hit, chunks, refs) for hit in case.expected]
    miss: ExpectedHit | None = None
    for hit, rank in zip(case.expected, ranks, strict=True):
        if rank is None:
            miss = hit
            break
    return CaseResult(
        case_id=case.case_id,
        mrr=reciprocal_rank(case, chunks, refs),
        recall_at={k: recall_at_k(case, chunks, refs, k) for k in k_values},
        per_hit_ranks=ranks,
        first_miss=miss,
    )


__all__ = [
    "AggregateMetrics",
    "CaseResult",
    "EvalReport",
    "aggregate",
    "first_matching_rank",
    "matches",
    "recall_at_k",
    "reciprocal_rank",
    "score_one",
]
