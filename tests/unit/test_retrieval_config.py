"""Unit tests for retrieval/config.py (Phase 5 slice 1)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cf_knowledge_kiln.retrieval.config import (
    DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_STATUS_WEIGHTS,
    RetrievalConfig,
    RetrievalConfigError,
    load_retrieval_config,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


class TestRetrievalConfigDefaults:
    def test_defaults_match_security_example(self) -> None:
        config = RetrievalConfig()
        assert config.status_weights == DEFAULT_STATUS_WEIGHTS
        assert config.stale_after_days == DEFAULT_STALE_AFTER_DAYS

    def test_unknown_status_gets_weight_one(self) -> None:
        """Forward-compat: a status not in the map doesn't zero-out matches."""
        config = RetrievalConfig()
        assert config.weight_for_status("future-status") == 1.0

    def test_known_status_returns_configured_weight(self) -> None:
        config = RetrievalConfig()
        assert config.weight_for_status("active") == 1.0
        assert config.weight_for_status("deprecated") == 0.2


class TestLoadRetrievalConfig:
    def test_none_path_returns_defaults(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            config = load_retrieval_config(None)
        assert config.status_weights == DEFAULT_STATUS_WEIGHTS

    def test_missing_file_returns_defaults_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            config = load_retrieval_config(tmp_path / "absent.yaml")
        assert config.status_weights == DEFAULT_STATUS_WEIGHTS
        assert any("no security config" in r.getMessage() for r in caplog.records)

    def test_loads_status_weights_from_yaml(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "security.yaml",
            """
retrieval:
  status_weights:
    active: 1.0
    draft: 0.3
freshness:
  stale_after_days: 30
""",
        )
        config = load_retrieval_config(path)
        assert config.status_weights == {"active": 1.0, "draft": 0.3}
        assert config.stale_after_days == 30

    def test_missing_sections_use_defaults(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "security.yaml", "content_filters: {}\n")
        config = load_retrieval_config(path)
        assert config.status_weights == DEFAULT_STATUS_WEIGHTS
        assert config.stale_after_days == DEFAULT_STALE_AFTER_DAYS

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        bad = _write(tmp_path / "security.yaml", ":\n  -not: valid: yaml\n")
        with pytest.raises(RetrievalConfigError, match="malformed YAML"):
            load_retrieval_config(bad)

    def test_stale_after_days_null_disables_check(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "security.yaml",
            "freshness:\n  stale_after_days: null\n",
        )
        config = load_retrieval_config(path)
        assert config.stale_after_days is None

    def test_real_example_config_loads(self) -> None:
        """The shipped example file must always parse — and round-trip cleanly."""
        example = Path(__file__).resolve().parents[2] / "config" / "security.example.yaml"
        assert example.exists(), f"missing fixture: {example}"
        config = load_retrieval_config(example)
        assert config.status_weights["active"] == 1.0
        assert config.stale_after_days == 365
