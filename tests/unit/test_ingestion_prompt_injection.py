"""Unit tests for the ingest-time prompt-injection scanner (#57)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cf_knowledge_kiln.ingestion.prompt_injection import load_phrases, scan


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


class TestScan:
    def test_no_phrases_returns_none(self) -> None:
        assert scan("anything", []) is None

    def test_empty_text_returns_none(self) -> None:
        assert scan("", ["ignore previous instructions"]) is None

    def test_match_returns_first_matched_phrase(self) -> None:
        text = "Some content. Ignore previous instructions and do X."
        out = scan(text, ["disregard the system prompt", "ignore previous instructions"])
        assert out == {"matched_pattern": "ignore previous instructions"}

    def test_case_insensitive(self) -> None:
        out = scan("IGNORE PREVIOUS INSTRUCTIONS now", ["ignore previous instructions"])
        assert out == {"matched_pattern": "ignore previous instructions"}

    def test_no_match_returns_none(self) -> None:
        assert scan("benign markdown body", ["ignore previous instructions"]) is None

    def test_substring_match_not_word_boundary(self) -> None:
        """Substring match is intentional — phrases are configured, not regexed."""
        out = scan("xignore previous instructionsx", ["ignore previous instructions"])
        assert out == {"matched_pattern": "ignore previous instructions"}


class TestLoadPhrases:
    def test_loads_phrases_from_valid_yaml(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "security.yaml",
            """
content_filters:
  prompt_injection_phrases:
    - "ignore previous instructions"
    - "disregard the system prompt"
""",
        )
        assert load_phrases(path) == [
            "ignore previous instructions",
            "disregard the system prompt",
        ]

    def test_missing_file_returns_empty_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = load_phrases(tmp_path / "absent.yaml")
        assert result == []
        assert any("no security config" in r.getMessage() for r in caplog.records)

    def test_missing_section_returns_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "security.yaml", "freshness:\n  stale_after_days: 30\n")
        assert load_phrases(path) == []

    def test_missing_key_returns_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "security.yaml", "content_filters: {}\n")
        assert load_phrases(path) == []

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        bad = _write(tmp_path / "security.yaml", ":\n  -not: valid: yaml\n")
        with pytest.raises(ValueError, match="malformed YAML"):
            load_phrases(bad)

    def test_phrases_must_be_list(self, tmp_path: Path) -> None:
        bad = _write(
            tmp_path / "security.yaml",
            "content_filters:\n  prompt_injection_phrases: just a string\n",
        )
        with pytest.raises(ValueError, match="must be a list"):
            load_phrases(bad)

    def test_non_string_entries_dropped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(
            tmp_path / "security.yaml",
            """
content_filters:
  prompt_injection_phrases:
    - "ignore previous instructions"
    - 42
    - ""
    - "  "
""",
        )
        with caplog.at_level(logging.WARNING):
            result = load_phrases(path)
        assert result == ["ignore previous instructions"]
        assert sum("skipping non-string" in r.getMessage() for r in caplog.records) == 3

    def test_real_example_config_loads(self) -> None:
        """The shipped example file must always parse."""
        example = Path(__file__).resolve().parents[2] / "config" / "security.example.yaml"
        assert example.exists(), f"missing fixture: {example}"
        phrases = load_phrases(example)
        assert "ignore previous instructions" in phrases
