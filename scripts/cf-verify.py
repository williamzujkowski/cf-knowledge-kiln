"""``make cf-verify`` — static checks against the CF-deploy contract.

The CF push iteration that produced #229, #240, #241, #242, #243 cost
days of round-trips because the upstream repo shipped configs that
'looked right' but failed at staging time on the live foundation.
Each fix added one piece of context the next operator now starts
with — and this script encodes those lessons as a local quality
gate that runs in seconds.

What it checks:

* ``manifest.yml`` parses and every app declares a ``command:``,
  ``memory:``, ``disk_quota:``, and a buildpack.
* Each app's ``command:`` script exists relative to the repo root
  and is executable.
* ``disk_quota`` >= 2G (the CF foundation default hard cap; smaller
  values fail the buildpack staging step for the kiln's installed
  size — see #242).
* The worker app's ``command:`` actually invokes the daemon — not
  the Phase-1 import-and-exit shim (#240).
* ``requirements.txt`` resolves with ``pip install --dry-run`` — the
  buildpack's first failure mode pre-#229.
* ``config/models.example.yaml`` + ``config/sources.example.yaml``
  load via the kiln's own loaders. The fall-back-to-example path
  (#241) only works if the examples load.
* ``scripts/start-*.sh`` pass ``bash -n`` syntax check.

Exit non-zero on any failure. Per-check verbose output so the next
operator sees the rationale, not just a pass/fail.

This is NOT a substitute for the actual ``cf push`` — it can't
predict foundation-specific quirks (service brokers, custom
buildpacks, network policies). But every check here is one that DID
fire on a real deploy and now catches the regression in seconds.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 — sandboxed, explicit args
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_memory_or_disk(value: str) -> int:
    """Parse a CF size string like ``2G`` / ``1024M`` into megabytes.

    CF accepts ``M`` / ``MB`` / ``G`` / ``GB`` (case-insensitive).
    Bare integers are interpreted as megabytes per CF convention.
    """
    s = str(value).strip().upper().rstrip("B")
    if s.endswith("G"):
        return int(s[:-1]) * 1024
    if s.endswith("M"):
        return int(s[:-1])
    if s.isdigit():
        return int(s)
    raise ValueError(f"unrecognized CF size value: {value!r}")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str, failures: list[str]) -> None:
    print(f"  ✗ {msg}")
    failures.append(msg)


def check_manifest(failures: list[str]) -> None:
    print("manifest.yml")
    path = REPO_ROOT / "manifest.yml"
    if not path.exists():
        _fail("manifest.yml does not exist", failures)
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        _fail(f"manifest.yml is not valid YAML: {exc}", failures)
        return
    _ok("parses as YAML")

    apps = data.get("applications") or []
    if not apps:
        _fail("manifest.yml has no applications:", failures)
        return
    _ok(f"declares {len(apps)} app(s)")

    for app in apps:
        name = app.get("name", "<unnamed>")
        _check_app(app, name, failures)


def _check_app(app: dict[str, Any], name: str, failures: list[str]) -> None:
    print(f"  app: {name}")
    # Required keys for any real CF deploy.
    for key in ("command", "memory", "disk_quota", "buildpacks"):
        if key not in app:
            _fail(f"{name}: missing required key {key!r}", failures)

    # The command must point at a real, executable script.
    cmd = app.get("command")
    if cmd:
        # ``cf push`` runs the command from the staged droplet root.
        # Convention in this repo: scripts/start-*.sh.
        script = cmd.split()[0]  # drop any trailing args
        script_path = REPO_ROOT / script.lstrip("./")
        if not script_path.exists():
            _fail(f"{name}: command script {script} does not exist", failures)
        elif not script_path.is_file():
            _fail(f"{name}: command path {script} is not a regular file", failures)
        else:
            mode = script_path.stat().st_mode
            if not (mode & 0o111):
                _fail(f"{name}: command script {script} is not executable", failures)
            else:
                _ok(f"command script {script} exists and is executable")

    # #242: disk_quota under 2G fails buildpack staging for the kiln's
    # installed footprint. The 2G hard cap is the CF foundation default
    # (cf-deployment's default_app_disk_in_mb).
    disk = app.get("disk_quota")
    if disk:
        try:
            mb = _parse_memory_or_disk(disk)
            if mb < 2048:
                _fail(
                    f"{name}: disk_quota={disk} (~{mb}M) is below the 2G minimum "
                    "needed for kiln staging — see #242",
                    failures,
                )
            else:
                _ok(f"disk_quota={disk} satisfies the 2G minimum (#242)")
        except ValueError as exc:
            _fail(f"{name}: cannot parse disk_quota={disk!r}: {exc}", failures)

    # #240: the worker's command must actually invoke the daemon. The
    # Phase-1 shim ``python -m cf_knowledge_kiln.ingestion.worker``
    # exits 0 immediately; CF treats that as a crashed start and
    # crash-loops. We detect the foot-gun by looking for the daemon
    # subcommand ``serve-worker`` in any worker app's command script.
    if "worker" in name.lower() and cmd:
        script_path = REPO_ROOT / cmd.split()[0].lstrip("./")
        if script_path.exists():
            body = script_path.read_text(encoding="utf-8")
            if "serve-worker" not in body:
                _fail(
                    f"{name}: command script {cmd} does not invoke 'serve-worker' — "
                    "may be the Phase-1 import-and-exit shim from #240",
                    failures,
                )
            else:
                _ok(f"{name}: command script invokes 'serve-worker' (no #240 regression)")


def check_requirements(failures: list[str]) -> None:
    print("requirements.txt")
    path = REPO_ROOT / "requirements.txt"
    if not path.exists():
        _fail("requirements.txt does not exist (#229)", failures)
        return
    _ok("exists")

    pip = shutil.which("pip") or shutil.which("pip3")
    if pip is None:
        _fail("no pip on PATH; cannot --dry-run resolve", failures)
        return
    try:
        # --dry-run resolves the dep graph without installing. Slow
        # (~30s on a cold cache) but catches version conflicts the
        # buildpack would otherwise hit at staging time.
        result = subprocess.run(  # noqa: S603 — explicit args, no shell
            [pip, "install", "--dry-run", "--quiet", "-r", str(path)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        _fail("pip --dry-run timed out after 5 min", failures)
        return
    if result.returncode != 0:
        _fail(
            f"pip --dry-run failed (exit {result.returncode}):\n"
            f"  stdout: {result.stdout[-500:]}\n"
            f"  stderr: {result.stderr[-500:]}",
            failures,
        )
    else:
        _ok("pip install --dry-run -r requirements.txt resolves")


def check_example_configs(failures: list[str]) -> None:
    print("config/*.example.yaml")
    # Add repo to path so we can import the loaders.
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from cf_knowledge_kiln.ingestion.embedding.factory import load_embedding_config
        from cf_knowledge_kiln.ingestion.sources import SourceAllowlist
    except ImportError as exc:
        _fail(f"can't import loaders: {exc}", failures)
        return

    models_example = REPO_ROOT / "config" / "models.example.yaml"
    if not models_example.exists():
        _fail(f"{models_example} does not exist", failures)
    else:
        try:
            load_embedding_config(models_example)
            _ok("models.example.yaml loads via load_embedding_config (#241)")
        except Exception as exc:  # noqa: BLE001 — surface all loader errors
            _fail(f"models.example.yaml fails to load: {exc}", failures)

    sources_example = REPO_ROOT / "config" / "sources.example.yaml"
    if not sources_example.exists():
        _fail(f"{sources_example} does not exist", failures)
    else:
        try:
            SourceAllowlist.from_yaml(sources_example)
            _ok("sources.example.yaml loads via SourceAllowlist.from_yaml (#241)")
        except Exception as exc:  # noqa: BLE001
            _fail(f"sources.example.yaml fails to load: {exc}", failures)


def check_start_scripts(failures: list[str]) -> None:
    print("scripts/start-*.sh")
    bash = shutil.which("bash")
    if bash is None:
        _fail("no bash on PATH; cannot syntax-check start scripts", failures)
        return
    for script in sorted((REPO_ROOT / "scripts").glob("start-*.sh")):
        try:
            result = subprocess.run(  # noqa: S603
                [bash, "-n", str(script)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _fail(f"{script.name}: bash -n timed out", failures)
            continue
        if result.returncode != 0:
            _fail(f"{script.name}: bash -n failed: {result.stderr.strip()}", failures)
        else:
            _ok(f"{script.name}: bash -n OK")


def main() -> int:
    print("cf-verify: static CF-deploy contract checks")
    print("=" * 56)
    failures: list[str] = []
    check_manifest(failures)
    check_requirements(failures)
    check_example_configs(failures)
    check_start_scripts(failures)
    print("=" * 56)
    if failures:
        print(f"cf-verify: FAIL ({len(failures)} check(s) failed)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("cf-verify: PASS")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
