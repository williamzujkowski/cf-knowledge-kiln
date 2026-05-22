"""Unit tests for the relevance re-grading aggregator (#165).

Covers the deterministic core of `tests/eval/regrade_review_precision.py`
— the median + consensus computation. The `worksheet` subcommand needs
a live DB + the embedding model, so it is exercised by running the
script, not here.
"""

from __future__ import annotations

from tests.eval.regrade_review_precision import (
    _consensus_summary,
    _render_relevance_blocks,
    aggregate_grades,
)


def test_median_of_odd_judge_count() -> None:
    judges = [
        {"case-a": {"doc#H": 3}},
        {"case-a": {"doc#H": 3}},
        {"case-a": {"doc#H": 2}},
    ]
    [pair] = aggregate_grades(judges)
    assert pair.case_id == "case-a"
    assert pair.citation == "doc#H"
    assert pair.grades == [3, 3, 2]
    assert pair.median == 3
    assert pair.spread == 1


def test_unanimous_pair_has_zero_spread() -> None:
    judges = [{"c": {"x": 2}}, {"c": {"x": 2}}, {"c": {"x": 2}}]
    [pair] = aggregate_grades(judges)
    assert pair.median == 2
    assert pair.spread == 0


def test_wide_disagreement_reported_in_spread() -> None:
    judges = [{"c": {"x": 0}}, {"c": {"x": 3}}, {"c": {"x": 1}}]
    [pair] = aggregate_grades(judges)
    assert pair.median == 1
    assert pair.spread == 3


def test_pair_graded_by_a_subset_of_judges_still_aggregates() -> None:
    """A judge that didn't grade a pair simply isn't counted for it."""
    judges = [
        {"c": {"x": 3, "y": 1}},
        {"c": {"x": 3}},  # judge 2 skipped y
        {"c": {"x": 2, "y": 1}},
    ]
    pairs = {(p.case_id, p.citation): p for p in aggregate_grades(judges)}
    assert pairs[("c", "x")].grades == [3, 3, 2]
    assert pairs[("c", "y")].grades == [1, 1]  # only the two judges who graded it
    assert pairs[("c", "y")].median == 1


def test_even_judge_count_rounds_the_median() -> None:
    judges = [{"c": {"x": 1}}, {"c": {"x": 2}}]
    [pair] = aggregate_grades(judges)
    # statistics.median([1, 2]) == 1.5 → rounded to an integer grade.
    assert pair.median in (1, 2)
    assert pair.spread == 1


def test_results_are_sorted_and_stable() -> None:
    judges = [{"b": {"z": 1}, "a": {"y": 2}}, {"b": {"z": 1}, "a": {"y": 2}}]
    pairs = aggregate_grades(judges)
    assert [(p.case_id, p.citation) for p in pairs] == [("a", "y"), ("b", "z")]


def test_consensus_summary_counts_unanimous_and_wide() -> None:
    judges = [
        {"c": {"x": 3, "y": 0, "z": 2}},
        {"c": {"x": 3, "y": 3, "z": 2}},
        {"c": {"x": 3, "y": 1, "z": 1}},
    ]
    pairs = aggregate_grades(judges)
    summary = _consensus_summary(pairs)
    # x is unanimous (3,3,3); y spreads 0..3 (>= 2); z spreads 1.
    assert "1/3 unanimous" in summary
    assert "1/3 with disagreement spread >= 2" in summary


def test_render_relevance_blocks_groups_by_case() -> None:
    judges = [{"case-a": {"doc#H": 3}}, {"case-a": {"doc#H": 3}}]
    block = _render_relevance_blocks(aggregate_grades(judges))
    assert "case-a:" in block
    assert "relevance:" in block
    assert '"doc#H": 3' in block
