"""Tests for the ingestion CLI's exit-code contract (#13 acceptance)."""

from __future__ import annotations

from pathlib import Path

import yaml

from cf_knowledge_kiln.ingestion.cli import main


def _write(tmp_path: Path, payload: object) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(yaml.safe_dump(payload))
    return p


def test_cli_validate_returns_zero_on_valid_yaml(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": [{"name": "x", "type": "git", "repo": "o/r"}]})
    assert main(["--config", str(p), "validate"]) == 0


def test_cli_validate_returns_nonzero_on_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  - not: valid: yaml")
    assert main(["--config", str(bad)]) == 2


def test_cli_validate_returns_nonzero_on_schema_violation(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": [{"name": "x", "type": "http", "url": "https://e"}]})
    assert main(["--config", str(p), "validate"]) == 2


def test_cli_validate_returns_nonzero_on_missing_file(tmp_path: Path) -> None:
    assert main(["--config", str(tmp_path / "nope.yaml"), "validate"]) == 2


def test_cli_default_command_is_validate(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": []})
    assert main(["--config", str(p)]) == 0


def test_cli_unwired_command_returns_nonzero(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": []})
    assert main(["--config", str(p), "ingest"]) == 2
