"""Unit tests for the sensitive-content scanner (#100)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cf_knowledge_kiln.ingestion.sensitive_content import (
    _CompiledPattern,
    load_patterns,
    scan,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "security.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestLoadPatterns:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_patterns(tmp_path / "nope.yaml") == []

    def test_no_patterns_section_returns_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "content_filters: {}\n")
        assert load_patterns(path) == []

    def test_compiles_valid_patterns(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            (
                "content_filters:\n"
                "  sensitive_patterns:\n"
                "    - 'AKIA[0-9A-Z]{16}'\n"
                "    - 'xox[baprs]-[0-9a-zA-Z]{10,}'\n"
            ),
        )
        out = load_patterns(path)
        assert len(out) == 2
        assert out[0].source == "AKIA[0-9A-Z]{16}"
        assert out[1].compiled.search("xoxb-1234567890abc") is not None

    def test_skips_invalid_regex(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = _write(
            tmp_path,
            (
                "content_filters:\n"
                "  sensitive_patterns:\n"
                "    - 'AKIA[0-9A-Z]{16}'\n"
                "    - '[invalid('\n"  # unbalanced
                "    - 'xoxp-.*'\n"
            ),
        )
        out = load_patterns(path)
        assert [p.source for p in out] == ["AKIA[0-9A-Z]{16}", "xoxp-.*"]

    def test_skips_non_string_entries(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            (
                "content_filters:\n"
                "  sensitive_patterns:\n"
                "    - 'AKIA[0-9A-Z]{16}'\n"
                "    - 42\n"
                "    - ''\n"
            ),
        )
        out = load_patterns(path)
        assert [p.source for p in out] == ["AKIA[0-9A-Z]{16}"]

    def test_rejects_non_list_patterns(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "content_filters:\n  sensitive_patterns: not-a-list\n",
        )
        with pytest.raises(ValueError, match="must be a list"):
            load_patterns(path)


class TestScan:
    def _patterns(self, *sources: str) -> list[_CompiledPattern]:
        import re

        return [_CompiledPattern(source=s, compiled=re.compile(s)) for s in sources]

    def test_matches_aws_key(self) -> None:
        patterns = self._patterns("AKIA[0-9A-Z]{16}")
        out = scan("token: AKIAIOSFODNN7EXAMPLE in config", patterns)  # pragma: allowlist secret
        assert out == {"matched_pattern": "AKIA[0-9A-Z]{16}"}

    def test_returns_first_match_only(self) -> None:
        patterns = self._patterns("foo", "bar")
        out = scan("foo and bar", patterns)
        assert out == {"matched_pattern": "foo"}

    def test_no_match_returns_none(self) -> None:
        patterns = self._patterns("AKIA[0-9A-Z]{16}")
        assert scan("nothing sensitive here", patterns) is None

    def test_empty_inputs_short_circuit(self) -> None:
        assert scan("", self._patterns("foo")) is None
        assert scan("foo", []) is None
