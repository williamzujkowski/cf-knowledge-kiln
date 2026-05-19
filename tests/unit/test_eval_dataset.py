"""Unit tests for eval golden-set loading (#31)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cf_knowledge_kiln.eval.dataset import GoldenSetError, load_golden_set, load_review_set


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "golden.yaml"
    p.write_text(textwrap.dedent(body))
    return p


class TestLoadGoldenSet:
    def test_parses_minimal_valid_case(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            version: 1
            cases:
              - case_id: c1
                query: "alpha"
                expected:
                  - repo: r
                    path: docs/x.md
            """,
        )
        cases = load_golden_set(path)
        assert len(cases) == 1
        c = cases[0]
        assert c.case_id == "c1"
        assert c.query == "alpha"
        assert c.filters == {}
        assert c.expected[0].repo == "r"
        assert c.expected[0].path == "docs/x.md"
        assert c.expected[0].heading_path == []
        assert c.expected[0].must_appear_within_k == 10

    def test_filters_are_passthrough(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                filters: {status: [active]}
                expected:
                  - {repo: r, path: p}
            """,
        )
        cases = load_golden_set(path)
        assert cases[0].filters == {"status": ["active"]}

    def test_rejects_duplicate_case_ids(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected: [{repo: r, path: p}]
              - case_id: c1
                query: q2
                expected: [{repo: r, path: q}]
            """,
        )
        with pytest.raises(GoldenSetError, match="duplicate"):
            load_golden_set(path)

    def test_rejects_missing_cases_key(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "version: 1\n")
        with pytest.raises(GoldenSetError, match="cases"):
            load_golden_set(path)

    def test_rejects_empty_expected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected: []
            """,
        )
        with pytest.raises(GoldenSetError, match="at least one expected hit"):
            load_golden_set(path)

    def test_rejects_missing_required_hit_fields(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected:
                  - heading_path: [a]
            """,
        )
        with pytest.raises(GoldenSetError, match="missing 'repo'"):
            load_golden_set(path)

    def test_rejects_non_positive_must_appear_within_k(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected:
                  - repo: r
                    path: p
                    must_appear_within_k: 0
            """,
        )
        with pytest.raises(GoldenSetError, match="must_appear_within_k"):
            load_golden_set(path)

    def test_rejects_unsupported_version(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            version: 99
            cases:
              - case_id: c1
                query: q
                expected: [{repo: r, path: p}]
            """,
        )
        with pytest.raises(GoldenSetError, match="unsupported schema version"):
            load_golden_set(path)

    def test_rejects_invalid_filters_with_case_attribution(self, tmp_path: Path) -> None:
        """Pydantic errors in filters surface with the case_id, not a raw trace."""
        path = _write(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                filters: {status: "not-a-list"}
                expected: [{repo: r, path: p}]
            """,
        )
        with pytest.raises(GoldenSetError, match="case 'c1' has invalid filters"):
            load_golden_set(path)

    def test_rejects_bad_heading_path_type(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected:
                  - repo: r
                    path: p
                    heading_path: "not-a-list"
            """,
        )
        with pytest.raises(GoldenSetError, match="heading_path"):
            load_golden_set(path)


# ─── Review-set loader (#108 item 2 — multi-relevance schema) ─────────


def _write_review(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "review.yaml"
    p.write_text(textwrap.dedent(body))
    return p


class TestLoadReviewSetRelevance:
    """The optional ``relevance`` field on :class:`ReviewCase` (#108 item 2).

    Backward compatibility: cases without ``relevance:`` continue to
    parse cleanly with an empty dict default. Validation: grades must
    be ints in ``[0, 3]`` and keys must be strings; anything else
    raises :class:`GoldenSetError` with the case_id attributed.
    """

    def test_parses_case_without_relevance(self, tmp_path: Path) -> None:
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
            """,
        )
        cases = load_review_set(path)
        assert len(cases) == 1
        assert cases[0].relevance == {}

    def test_parses_case_with_relevance(self, tmp_path: Path) -> None:
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
                relevance:
                  "kiln-eval/foo.md#H1": 3
                  "kiln-eval/foo.md#H2": 2
                  "kiln-eval/bar.md#H1": 0
            """,
        )
        cases = load_review_set(path)
        assert cases[0].relevance == {
            "kiln-eval/foo.md#H1": 3,
            "kiln-eval/foo.md#H2": 2,
            "kiln-eval/bar.md#H1": 0,
        }

    def test_rejects_non_mapping_relevance(self, tmp_path: Path) -> None:
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
                relevance:
                  - "not"
                  - "a-mapping"
            """,
        )
        with pytest.raises(GoldenSetError, match="case 'c1' relevance must be a mapping"):
            load_review_set(path)

    def test_rejects_relevance_grade_out_of_range(self, tmp_path: Path) -> None:
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
                relevance:
                  "kiln-eval/foo.md#H1": 4
            """,
        )
        with pytest.raises(GoldenSetError, match="grade must be an int in"):
            load_review_set(path)

    def test_rejects_negative_grade(self, tmp_path: Path) -> None:
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
                relevance:
                  "kiln-eval/foo.md#H1": -1
            """,
        )
        with pytest.raises(GoldenSetError, match="grade must be an int in"):
            load_review_set(path)

    def test_rejects_non_int_grade(self, tmp_path: Path) -> None:
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
                relevance:
                  "kiln-eval/foo.md#H1": "high"
            """,
        )
        with pytest.raises(GoldenSetError, match="grade must be an int in"):
            load_review_set(path)

    def test_rejects_bool_grade(self, tmp_path: Path) -> None:
        """``True``/``False`` are ints in Python; the loader must reject them.

        YAML parses ``true``/``false`` as bools. Accepting a bool here
        would let ``relevance: {key: true}`` silently mean grade=1,
        which is exactly the silent failure mode the codebase forbids.
        """
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
                relevance:
                  "kiln-eval/foo.md#H1": true
            """,
        )
        with pytest.raises(GoldenSetError, match="grade must be an int in"):
            load_review_set(path)

    def test_rejects_non_string_key(self, tmp_path: Path) -> None:
        path = _write_review(
            tmp_path,
            """\
            cases:
              - case_id: c1
                query: q
                expected_review: false
                relevance:
                  123: 2
            """,
        )
        with pytest.raises(GoldenSetError, match="relevance keys must be strings"):
            load_review_set(path)
