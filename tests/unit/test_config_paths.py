"""Unit tests for :mod:`cf_knowledge_kiln.config.paths` (#241).

The fallback policy is what these tests pin down: configured path
wins when it exists; missing path falls back to the ``.example``
sibling; both missing returns the original (caller's existing
"file not found" handling fires).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cf_knowledge_kiln.config.paths import (
    _FALLBACK_LOGGED,
    resolve_with_example_fallback,
)


@pytest.fixture(autouse=True)
def _clear_fallback_log_cache() -> None:
    """Each test starts with a clean log-cache so 'one warning per path'
    assertions don't bleed across tests."""
    _FALLBACK_LOGGED.clear()


def test_returns_path_unchanged_when_it_exists(tmp_path: Path) -> None:
    p = tmp_path / "real.yaml"
    p.write_text("x: 1\n")
    assert resolve_with_example_fallback(p) == p


def test_falls_back_to_example_sibling_when_configured_missing(tmp_path: Path) -> None:
    configured = tmp_path / "models.yaml"
    example = tmp_path / "models.example.yaml"
    example.write_text("models: {}\n")
    assert resolve_with_example_fallback(configured) == example


def test_returns_original_when_neither_exists(tmp_path: Path) -> None:
    """Caller's file-not-found handling MUST still fire if both are missing."""
    configured = tmp_path / "nowhere.yaml"
    out = resolve_with_example_fallback(configured)
    assert out == configured
    assert not out.exists()


def test_logs_warning_once_per_path_on_substitution(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-resolving the same path shouldn't spam the log on a polling
    daemon — operators see the nudge once, not every 5 seconds."""
    configured = tmp_path / "models.yaml"
    example = tmp_path / "models.example.yaml"
    example.write_text("models: {}\n")
    with caplog.at_level(logging.WARNING, logger="cf_knowledge_kiln.config.paths"):
        resolve_with_example_fallback(configured)
        resolve_with_example_fallback(configured)
        resolve_with_example_fallback(configured)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "falling back" in warnings[0].message


def test_logs_warning_per_distinct_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Different missing files each get their own one-time warning."""
    a_configured = tmp_path / "models.yaml"
    a_example = tmp_path / "models.example.yaml"
    a_example.write_text("models: {}\n")
    b_configured = tmp_path / "sources.yaml"
    b_example = tmp_path / "sources.example.yaml"
    b_example.write_text("sources: []\n")
    with caplog.at_level(logging.WARNING, logger="cf_knowledge_kiln.config.paths"):
        resolve_with_example_fallback(a_configured)
        resolve_with_example_fallback(b_configured)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_accepts_string_input(tmp_path: Path) -> None:
    """Pydantic settings give us ``str`` from env vars — helper must accept."""
    p = tmp_path / "real.yaml"
    p.write_text("x: 1\n")
    assert resolve_with_example_fallback(str(p)) == p


def test_no_warning_when_configured_file_exists(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Happy path: configured file is present → no log, no fallback."""
    p = tmp_path / "real.yaml"
    p.write_text("x: 1\n")
    example = tmp_path / "real.example.yaml"
    example.write_text("x: 2\n")  # present but unused
    with caplog.at_level(logging.WARNING, logger="cf_knowledge_kiln.config.paths"):
        out = resolve_with_example_fallback(p)
    assert out == p  # not the example
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_shipped_example_files_resolve(tmp_path: Path) -> None:
    """Regression test for the actual shipped example files.

    A fresh checkout with no operator-customized config should
    resolve both default settings paths to existing files.
    """
    # cd to repo root and use the real defaults
    repo_root = Path(__file__).resolve().parents[2]
    models = resolve_with_example_fallback(repo_root / "config" / "models.yaml")
    sources = resolve_with_example_fallback(repo_root / "config" / "sources.yaml")
    # Both should resolve to .example.yaml siblings that exist
    assert models.exists(), f"models config did not resolve: {models}"
    assert sources.exists(), f"sources config did not resolve: {sources}"
