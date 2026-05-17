"""Unit tests for eval golden-set loading (#31)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cf_knowledge_kiln.eval.dataset import GoldenSetError, load_golden_set


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
