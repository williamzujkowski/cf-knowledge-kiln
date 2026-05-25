"""Tests for the ingestion CLI's exit-code contract (#13 acceptance)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cf_knowledge_kiln.ingestion.cli import _default_config_path, main


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


# ─── #243: --config defaults from KILN_SOURCE_ALLOWLIST_PATH env var ──


class TestSourceAllowlistEnvDefault:
    """The worker daemon + API + ``make ingest`` must all read one
    setting so a CF deploy's env block actually steers ingestion."""

    def test_default_is_config_sources_yaml_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KILN_SOURCE_ALLOWLIST_PATH", raising=False)
        assert _default_config_path() == Path("config/sources.yaml")

    def test_default_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_SOURCE_ALLOWLIST_PATH", "/custom/sources.yaml")
        assert _default_config_path() == Path("/custom/sources.yaml")

    def test_cli_validates_against_env_var_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: env var points at a real file, no ``--config`` passed,
        CLI validates that file (exits 0)."""
        p = _write(tmp_path, {"sources": []})
        monkeypatch.setenv("KILN_SOURCE_ALLOWLIST_PATH", str(p))
        assert main(["validate"]) == 0

    def test_explicit_flag_overrides_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--config`` wins over the env var — operators can still
        override per invocation."""
        good = _write(tmp_path, {"sources": []})
        bad = tmp_path / "missing.yaml"
        monkeypatch.setenv("KILN_SOURCE_ALLOWLIST_PATH", str(bad))
        # If env var won, this would exit 2 on missing file.
        assert main(["--config", str(good), "validate"]) == 0
