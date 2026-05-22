"""Unit tests for retrieval/config.py (Phase 5 slice 1)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cf_knowledge_kiln.retrieval.config import (
    DEFAULT_MAX_WARNING_RANK,
    DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_STATUS_WEIGHTS,
    DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD,
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

    def test_loads_weak_evidence_score_threshold(self, tmp_path: Path) -> None:
        """#160 — operators can override the threshold via security.yaml."""
        path = _write(
            tmp_path / "security.yaml",
            "retrieval:\n  weak_evidence_score_threshold: 0.03\n",
        )
        config = load_retrieval_config(path)
        assert config.weak_evidence_score_threshold == 0.03

    def test_weak_evidence_score_threshold_rejects_zero(self, tmp_path: Path) -> None:
        """A zero floor would mark every result as weak; pydantic gt=0 rejects."""
        path = _write(
            tmp_path / "security.yaml",
            "retrieval:\n  weak_evidence_score_threshold: 0.0\n",
        )
        with pytest.raises(RetrievalConfigError):
            load_retrieval_config(path)

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

    def test_rejects_zero_stale_after_days(self, tmp_path: Path) -> None:
        """#172: stale_after_days=0 makes the freshness math divide by zero."""
        path = _write(
            tmp_path / "security.yaml",
            "freshness:\n  stale_after_days: 0\n",
        )
        with pytest.raises(RetrievalConfigError):
            load_retrieval_config(path)

    def test_rejects_negative_stale_after_days(self, tmp_path: Path) -> None:
        """#172: a negative window inverts the freshness factor — older docs
        would score *higher*. Refuse it at config-load time."""
        path = _write(
            tmp_path / "security.yaml",
            "freshness:\n  stale_after_days: -30\n",
        )
        with pytest.raises(RetrievalConfigError):
            load_retrieval_config(path)

    def test_rejects_negative_status_weight(self, tmp_path: Path) -> None:
        """A negative weight would invert ranking; refuse at load time."""
        path = _write(
            tmp_path / "security.yaml",
            "retrieval:\n  status_weights:\n    active: -0.1\n",
        )
        with pytest.raises(RetrievalConfigError, match="outside"):
            load_retrieval_config(path)

    def test_rejects_zero_status_weight(self, tmp_path: Path) -> None:
        """Zero would silently zero-out matches — caller should use a tiny + value."""
        path = _write(
            tmp_path / "security.yaml",
            "retrieval:\n  status_weights:\n    deprecated: 0\n",
        )
        with pytest.raises(RetrievalConfigError, match="outside"):
            load_retrieval_config(path)

    def test_rejects_status_weight_above_one(self, tmp_path: Path) -> None:
        """Weights > 1.0 break the multiplier semantics."""
        path = _write(
            tmp_path / "security.yaml",
            "retrieval:\n  status_weights:\n    active: 1.5\n",
        )
        with pytest.raises(RetrievalConfigError, match="outside"):
            load_retrieval_config(path)

    def test_real_example_config_loads(self) -> None:
        """The shipped example file must always parse — and round-trip cleanly."""
        example = Path(__file__).resolve().parents[2] / "config" / "security.example.yaml"
        assert example.exists(), f"missing fixture: {example}"
        config = load_retrieval_config(example)
        assert config.status_weights["active"] == 1.0
        assert config.stale_after_days == 365
        # #161: example must include max_warning_rank so the policy is
        # discoverable from the shipped config.
        assert config.max_warning_rank == DEFAULT_MAX_WARNING_RANK


class TestRelevanceAwareWarningKnobs:
    """#161 — relevance_floor + max_warning_rank loaders + defaults."""

    def test_defaults_track_weak_evidence_threshold(self) -> None:
        """`relevance_floor=None` means 'use weak_evidence_score_threshold'."""
        config = RetrievalConfig()
        assert config.relevance_floor is None
        assert config.effective_relevance_floor == DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD
        assert config.max_warning_rank == DEFAULT_MAX_WARNING_RANK

    def test_explicit_relevance_floor_overrides_default(self) -> None:
        config = RetrievalConfig(relevance_floor=0.05)
        assert config.effective_relevance_floor == 0.05

    def test_explicit_relevance_floor_and_threshold_diverge_cleanly(self) -> None:
        """An operator can demand a STRICTER floor on warnings vs. weak-evidence."""
        config = RetrievalConfig(
            weak_evidence_score_threshold=0.46,
            relevance_floor=0.69,  # 1.5x
        )
        assert config.weak_evidence_score_threshold == 0.46
        assert config.effective_relevance_floor == 0.69

    def test_loads_relevance_floor_and_max_warning_rank_from_yaml(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "security.yaml",
            ("retrieval:\n  relevance_floor: 0.69\n  max_warning_rank: 5\n"),
        )
        config = load_retrieval_config(path)
        assert config.relevance_floor == 0.69
        assert config.max_warning_rank == 5

    def test_max_warning_rank_below_one_rejected(self, tmp_path: Path) -> None:
        """A 0 or negative cutoff would disable per-chunk warnings entirely."""
        path = _write(
            tmp_path / "security.yaml",
            "retrieval:\n  max_warning_rank: 0\n",
        )
        with pytest.raises(RetrievalConfigError):
            load_retrieval_config(path)

    def test_relevance_floor_zero_rejected(self, tmp_path: Path) -> None:
        """Like weak_evidence: a zero floor is a footgun; refuse at load time."""
        path = _write(
            tmp_path / "security.yaml",
            "retrieval:\n  relevance_floor: 0.0\n",
        )
        with pytest.raises(RetrievalConfigError):
            load_retrieval_config(path)
