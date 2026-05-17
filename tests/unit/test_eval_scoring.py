"""Unit tests for eval scoring functions (#31).

All pure: no DB, no network. We hand-build :class:`RankedChunk` lists
and a tiny ref-style mapping so each test pins one behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from cf_knowledge_kiln.eval.dataset import ExpectedHit, GoldenCase
from cf_knowledge_kiln.eval.scoring import (
    aggregate,
    first_matching_rank,
    matches,
    recall_at_k,
    reciprocal_rank,
    score_one,
)
from cf_knowledge_kiln.retrieval.ranking import RankedChunk


@dataclass
class _StubRef:
    """Minimal DocumentRef-shaped stub — scoring only reads repo + path."""

    repo: str | None
    path: str | None


def _chunk(doc_id: UUID, heading: tuple[str, ...] = (), status: str = "active") -> RankedChunk:
    return RankedChunk(
        chunk_id=uuid4(),
        document_id=doc_id,
        score=0.5,
        status=status,
        heading_path=heading,
    )


class TestMatches:
    def test_match_on_repo_and_path(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        hit = ExpectedHit(repo="r", path="x.md")
        assert matches(hit, _chunk(doc), refs) is True

    def test_repo_mismatch_fails(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="other", path="x.md")}
        hit = ExpectedHit(repo="r", path="x.md")
        assert matches(hit, _chunk(doc), refs) is False

    def test_path_mismatch_fails(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="other.md")}
        hit = ExpectedHit(repo="r", path="x.md")
        assert matches(hit, _chunk(doc), refs) is False

    def test_heading_path_strict_equality(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        hit = ExpectedHit(repo="r", path="x.md", heading_path=["A", "B"])
        assert matches(hit, _chunk(doc, heading=("A", "B")), refs) is True
        assert matches(hit, _chunk(doc, heading=("A",)), refs) is False
        assert matches(hit, _chunk(doc, heading=("A", "B", "C")), refs) is False

    def test_empty_heading_path_is_doc_wildcard(self) -> None:
        """``heading_path: []`` matches any chunk in the document."""
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        hit = ExpectedHit(repo="r", path="x.md", heading_path=[])
        assert matches(hit, _chunk(doc, heading=("Anything",)), refs) is True
        assert matches(hit, _chunk(doc, heading=()), refs) is True

    def test_missing_ref_does_not_match(self) -> None:
        hit = ExpectedHit(repo="r", path="x.md")
        assert matches(hit, _chunk(uuid4()), refs={}) is False


class TestFirstMatchingRank:
    def test_returns_zero_indexed_rank(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        chunks = [_chunk(uuid4()), _chunk(uuid4()), _chunk(doc)]
        hit = ExpectedHit(repo="r", path="x.md")
        # doc is at index 2 — but unknown docs have no refs entry, so
        # the match search lands on the doc that does match.
        assert first_matching_rank(hit, chunks, refs) == 2

    def test_none_when_absent(self) -> None:
        hit = ExpectedHit(repo="r", path="x.md")
        chunks = [_chunk(uuid4()) for _ in range(3)]
        assert first_matching_rank(hit, chunks, refs={}) is None


class TestRecallAtK:
    def test_one_hit_one_relevant_one(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[ExpectedHit(repo="r", path="x.md")],
        )
        chunks = [_chunk(doc), _chunk(uuid4())]
        assert recall_at_k(case, chunks, refs, k=10) == 1.0
        assert recall_at_k(case, chunks, refs, k=1) == 1.0

    def test_partial_recall_across_multiple_expected(self) -> None:
        d1, d2 = uuid4(), uuid4()
        refs = {
            d1: _StubRef(repo="r", path="a.md"),
            d2: _StubRef(repo="r", path="b.md"),
        }
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[
                ExpectedHit(repo="r", path="a.md"),
                ExpectedHit(repo="r", path="b.md"),
            ],
        )
        # Only "a.md" is in the ranked list — recall = 0.5.
        chunks = [_chunk(d1)]
        assert recall_at_k(case, chunks, refs, k=10) == 0.5

    def test_zero_when_k_is_zero(self) -> None:
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[ExpectedHit(repo="r", path="x.md")],
        )
        assert recall_at_k(case, [], {}, k=0) == 0.0

    def test_below_k_does_not_count(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[ExpectedHit(repo="r", path="x.md")],
        )
        # Target is at rank 5 (0-indexed). Recall@5 should be 0 (rank 5
        # is the 6th item), recall@6 should be 1.
        chunks = [_chunk(uuid4()) for _ in range(5)] + [_chunk(doc)]
        assert recall_at_k(case, chunks, refs, k=5) == 0.0
        assert recall_at_k(case, chunks, refs, k=6) == 1.0


class TestReciprocalRank:
    def test_first_position_is_one(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[ExpectedHit(repo="r", path="x.md")],
        )
        assert reciprocal_rank(case, [_chunk(doc)], refs) == 1.0

    def test_third_position_is_one_third(self) -> None:
        doc = uuid4()
        refs = {doc: _StubRef(repo="r", path="x.md")}
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[ExpectedHit(repo="r", path="x.md")],
        )
        chunks = [_chunk(uuid4()), _chunk(uuid4()), _chunk(doc)]
        assert reciprocal_rank(case, chunks, refs) == 1 / 3

    def test_none_present_is_zero(self) -> None:
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[ExpectedHit(repo="r", path="x.md")],
        )
        assert reciprocal_rank(case, [_chunk(uuid4())], refs={}) == 0.0

    def test_picks_best_rank_across_multiple_hits(self) -> None:
        """MRR uses the earliest-ranked relevant item."""
        d1, d2 = uuid4(), uuid4()
        refs = {
            d1: _StubRef(repo="r", path="a.md"),
            d2: _StubRef(repo="r", path="b.md"),
        }
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[
                ExpectedHit(repo="r", path="a.md"),
                ExpectedHit(repo="r", path="b.md"),
            ],
        )
        # b at rank 0, a at rank 3 → RR uses 0, returns 1.0.
        chunks = [_chunk(d2), _chunk(uuid4()), _chunk(uuid4()), _chunk(d1)]
        assert reciprocal_rank(case, chunks, refs) == 1.0


class TestScoreOneAndAggregate:
    def test_score_one_populates_ranks_and_miss(self) -> None:
        d1, d2 = uuid4(), uuid4()
        refs = {
            d1: _StubRef(repo="r", path="a.md"),
            d2: _StubRef(repo="r", path="b.md"),
        }
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[
                ExpectedHit(repo="r", path="a.md"),
                ExpectedHit(repo="r", path="b.md"),
                ExpectedHit(repo="r", path="never.md"),
            ],
        )
        chunks = [_chunk(d2), _chunk(d1)]
        out = score_one(case, chunks, refs, k_values=(1, 5))
        assert out.case_id == "c"
        assert out.per_hit_ranks == [1, 0, None]
        assert out.first_miss is not None
        assert out.first_miss.path == "never.md"

    def test_aggregate_arithmetic_means(self) -> None:
        d = uuid4()
        refs = {d: _StubRef(repo="r", path="x.md")}
        case = GoldenCase(
            case_id="c",
            query="q",
            filters={},
            expected=[ExpectedHit(repo="r", path="x.md")],
        )
        a = score_one(case, [_chunk(d)], refs, k_values=(1, 5))
        b = score_one(case, [_chunk(uuid4()), _chunk(d)], refs, k_values=(1, 5))
        agg = aggregate([a, b], (1, 5))
        # MRR avg = (1.0 + 0.5) / 2 = 0.75
        assert agg.mrr == 0.75
        assert agg.recall_at[1] == 0.5  # a hits at k=1, b does not
        assert agg.recall_at[5] == 1.0
        assert agg.case_count == 2

    def test_aggregate_empty_returns_zeros(self) -> None:
        agg = aggregate([], (1, 5))
        assert agg.mrr == 0.0
        assert agg.recall_at == {1: 0.0, 5: 0.0}
        assert agg.case_count == 0
