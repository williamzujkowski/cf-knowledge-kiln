"""Unit tests for :mod:`scripts.cf-verify` (the CF-deploy contract checker).

We test the small parsing helpers directly. The integration shape
(running the whole script against the live repo) is covered by
``make cf-verify`` being part of ``make verify`` — if the contract
check breaks against the actual repo state, the local quality gate
catches it.

The point of these tests: lock in the exact failure-mode rationale
each check encodes, so a refactor doesn't accidentally weaken the
gate. Each test is the operator-readable explanation of why the
check exists in the first place.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cf-verify.py"


@pytest.fixture(scope="module")
def cf_verify() -> object:
    """Load scripts/cf-verify.py as a module via importlib.

    The script lives outside the package and isn't on the import path
    by default; we resolve it directly so unit tests don't need a
    sibling __init__.py or test conftest hack.
    """
    spec = importlib.util.spec_from_file_location("cf_verify", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cf_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestParseMemoryOrDisk:
    """``_parse_memory_or_disk`` normalizes CF size strings to MB."""

    def test_gigabyte_form(self, cf_verify: object) -> None:
        assert cf_verify._parse_memory_or_disk("2G") == 2048
        assert cf_verify._parse_memory_or_disk("2GB") == 2048
        assert cf_verify._parse_memory_or_disk("2g") == 2048

    def test_megabyte_form(self, cf_verify: object) -> None:
        assert cf_verify._parse_memory_or_disk("1024M") == 1024
        assert cf_verify._parse_memory_or_disk("1024MB") == 1024
        assert cf_verify._parse_memory_or_disk("1024m") == 1024

    def test_bare_integer_is_megabytes(self, cf_verify: object) -> None:
        """CF convention: bare ints are MB (matches cf push docs)."""
        assert cf_verify._parse_memory_or_disk("2048") == 2048

    def test_rejects_unparseable(self, cf_verify: object) -> None:
        with pytest.raises(ValueError, match="unrecognized"):
            cf_verify._parse_memory_or_disk("4T")
        with pytest.raises(ValueError):
            cf_verify._parse_memory_or_disk("garbage")


class TestCheckApp:
    """Per-app manifest checks — encode the actual failure modes
    that motivated #240 and #242."""

    def test_passes_on_valid_app(
        self, cf_verify: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf_verify, "REPO_ROOT", tmp_path)
        # Synth a minimal valid app + a stub script.
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "start-thing.sh"
        script.write_text("#!/bin/bash\nexec sleep 1\n")
        script.chmod(0o755)
        failures: list[str] = []
        cf_verify._check_app(
            {
                "name": "myapp",
                "command": "./scripts/start-thing.sh",
                "memory": "1G",
                "disk_quota": "2G",
                "buildpacks": ["python_buildpack"],
            },
            "myapp",
            failures,
        )
        assert failures == []

    def test_flags_missing_required_keys(
        self, cf_verify: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf_verify, "REPO_ROOT", tmp_path)
        failures: list[str] = []
        cf_verify._check_app({"name": "skinny"}, "skinny", failures)
        # Missing all four required keys.
        joined = " ".join(failures)
        assert "command" in joined
        assert "memory" in joined
        assert "disk_quota" in joined
        assert "buildpacks" in joined

    def test_flags_disk_quota_below_2g(
        self, cf_verify: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#242: 1G disk fails buildpack staging for the kiln footprint."""
        monkeypatch.setattr(cf_verify, "REPO_ROOT", tmp_path)
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "start-x.sh"
        script.write_text("#!/bin/bash\nexec serve-worker\n")
        script.chmod(0o755)
        failures: list[str] = []
        cf_verify._check_app(
            {
                "name": "small",
                "command": "./scripts/start-x.sh",
                "memory": "1G",
                "disk_quota": "1G",
                "buildpacks": ["python_buildpack"],
            },
            "small",
            failures,
        )
        assert any("2G minimum" in f or "#242" in f for f in failures)

    def test_flags_worker_without_serve_worker_invocation(
        self, cf_verify: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#240: a worker app whose script doesn't run 'serve-worker' is the
        Phase-1 import-and-exit shim that crash-loops on CF."""
        monkeypatch.setattr(cf_verify, "REPO_ROOT", tmp_path)
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "start-worker.sh"
        # The exact regression we want to catch: import-and-exit shim
        # without 'serve-worker' anywhere.
        script.write_text("#!/bin/bash\nexec python -m cf_knowledge_kiln.ingestion.worker\n")
        script.chmod(0o755)
        failures: list[str] = []
        cf_verify._check_app(
            {
                "name": "cf-knowledge-kiln-worker",
                "command": "./scripts/start-worker.sh",
                "memory": "2G",
                "disk_quota": "2G",
                "buildpacks": ["python_buildpack"],
            },
            "cf-knowledge-kiln-worker",
            failures,
        )
        assert any("serve-worker" in f and "#240" in f for f in failures)

    def test_flags_missing_command_script(
        self, cf_verify: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A command pointing at a nonexistent script must fail loudly."""
        monkeypatch.setattr(cf_verify, "REPO_ROOT", tmp_path)
        failures: list[str] = []
        cf_verify._check_app(
            {
                "name": "phantom",
                "command": "./scripts/does-not-exist.sh",
                "memory": "1G",
                "disk_quota": "2G",
                "buildpacks": ["python_buildpack"],
            },
            "phantom",
            failures,
        )
        assert any("does not exist" in f for f in failures)
