"""Source connectors: git (shallow clone) + local (directory walk).

Each connector returns a :class:`FetchResult` containing the files it
fetched and a list of :class:`SkippedFile` records with typed reasons
(matching the plan's skip-reason enum). Cap violations on the total
repo size or file count raise :class:`IngestionCapExceeded` — the
ingestion job should refuse the source rather than partially indexing.

The git connector uses ``git clone --depth=1`` into a temp directory
and reads files from there. It does not retain the cloned repo across
calls. Authentication is whatever the local ``git`` credential helper
provides; this module does not handle credentials directly. Local file
URLs (``file://``) work for tests without a network.

Only Markdown files (``.md`` / ``.markdown``) are fetched. Other file
types are skipped with reason ``unsupported_file_type``. Future Phase 3
extensions (e.g. plain-text, AsciiDoc) plug in by extending
``_SUPPORTED_SUFFIXES``.
"""

from __future__ import annotations

import glob
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from cf_knowledge_kiln.ingestion.sources import GitSource, LocalSource, Source

logger = logging.getLogger(__name__)

SkipReason = Literal[
    "excluded_by_pattern",
    "unsupported_file_type",
    "too_large",
    "binary_content",
    "symlink_escape",
]

_SUPPORTED_SUFFIXES: Final = frozenset({".md", ".markdown"})

# Default-deny patterns: paths whose first segment matches any of these
# are skipped regardless of include/exclude config. Prevents secrets in
# upstream repos (`.env`, `.git/config`, IDE state, etc.) from being
# silently indexed when a source's include defaults to `**/*`.
_DOTFILE_DENY: Final = (
    ".git",
    ".github",
    ".env",
    ".envrc",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
)


def _is_denied_by_default(rel_path: str) -> bool:
    """Top-level dotfile / vendored-tree paths are skipped pre-glob."""
    head, _, _ = rel_path.partition("/")
    if head in _DOTFILE_DENY:
        return True
    # Also block dotfiles at any depth (e.g. `docs/.env`).
    return any(seg.startswith(".") and seg not in {".", ".."} for seg in rel_path.split("/"))


@dataclass(frozen=True)
class IngestionCaps:
    """Per-source caps for connector behavior."""

    max_file_bytes: int
    max_files: int
    max_repo_bytes: int


@dataclass(frozen=True)
class FetchedFile:
    """One file pulled from a source."""

    path: str
    content: bytes
    size_bytes: int
    commit_sha: str | None = None


@dataclass(frozen=True)
class SkippedFile:
    """One file the connector refused to process. ``detail`` is human-readable."""

    path: str
    reason: SkipReason
    detail: str | None = None


@dataclass
class FetchResult:
    """The result of one source fetch.

    ``commit_sha`` is set for git sources only. ``skipped`` records
    every file the connector saw but did not return; the worker
    forwards these to the ``ingestion_runs`` summary.
    """

    files: list[FetchedFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    commit_sha: str | None = None


class IngestionCapExceeded(RuntimeError):
    """Raised when a source's cumulative size or file count breaches a cap."""


def _expand_globs(root: Path, patterns: Iterable[str]) -> set[str]:
    """Resolve glob patterns (with ``**`` recursion) against ``root``.

    Returns relative POSIX paths. Uses :mod:`glob` because ``fnmatch``
    does not understand ``**``-style recursive globbing, and
    ``Path.glob`` in 3.12 does not match the way we need.
    """
    expanded: set[str] = set()
    for pat in patterns:
        for match in glob.glob(pat, root_dir=str(root), recursive=True):  # noqa: PTH207
            expanded.add(Path(match).as_posix())
    return expanded


def _walk(root: Path, source: Source, caps: IngestionCaps, commit_sha: str | None) -> FetchResult:
    """Walk ``root`` and produce a :class:`FetchResult`.

    Enforces include/exclude globs, per-file size cap, total repo size
    cap, and file-count cap. Raises :class:`IngestionCapExceeded` when
    a cumulative cap is breached mid-walk.
    """
    include_set = _expand_globs(root, source.include or ["**/*"])
    exclude_set = _expand_globs(root, source.exclude or [])
    result = FetchResult(commit_sha=commit_sha)
    total_bytes = 0
    file_count = 0
    root_resolved = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        # rglob follows symlinks; verify the resolved target still lives
        # under root so a symlink can't reach /etc/passwd (or anything else
        # the worker UID can read) from inside the allowlisted source.
        try:
            path.resolve(strict=True).relative_to(root_resolved)
        except (ValueError, FileNotFoundError):
            result.skipped.append(
                SkippedFile(rel, "symlink_escape", "symlink target outside source root")
            )
            continue
        if _is_denied_by_default(rel):
            result.skipped.append(
                SkippedFile(rel, "excluded_by_pattern", "dotfile / vendored-tree default-deny")
            )
            continue
        if rel not in include_set:
            result.skipped.append(SkippedFile(rel, "excluded_by_pattern", "not in include glob"))
            continue
        if rel in exclude_set:
            result.skipped.append(SkippedFile(rel, "excluded_by_pattern", "matched exclude glob"))
            continue
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            result.skipped.append(
                SkippedFile(rel, "unsupported_file_type", path.suffix or "(no suffix)")
            )
            continue
        size = path.stat().st_size
        if size > caps.max_file_bytes:
            result.skipped.append(SkippedFile(rel, "too_large", f"{size} > {caps.max_file_bytes}"))
            continue
        total_bytes += size
        if total_bytes > caps.max_repo_bytes:
            raise IngestionCapExceeded(
                f"source {source.name!r} total size {total_bytes} > cap "
                f"{caps.max_repo_bytes}; aborting before indexing"
            )
        file_count += 1
        if file_count > caps.max_files:
            raise IngestionCapExceeded(
                f"source {source.name!r} file count {file_count} > cap "
                f"{caps.max_files}; aborting before indexing"
            )
        result.files.append(
            FetchedFile(
                path=rel,
                content=path.read_bytes(),
                size_bytes=size,
                commit_sha=commit_sha,
            )
        )
    return result


class LocalConnector:
    """Walk a local directory."""

    def __init__(self, caps: IngestionCaps) -> None:
        self._caps = caps

    def fetch(self, source: LocalSource) -> FetchResult:
        root = Path(source.path)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(
                f"local source {source.name!r} path does not exist or is not a directory: {root}"
            )
        return _walk(root, source, self._caps, commit_sha=None)


class GitConnector:
    """Shallow-clone a git source and walk the working tree."""

    def __init__(self, caps: IngestionCaps) -> None:
        self._caps = caps

    def fetch(self, source: GitSource) -> FetchResult:
        with tempfile.TemporaryDirectory(prefix="kiln-git-") as tmpdir:
            workdir = Path(tmpdir) / "repo"
            self._clone(source, workdir)
            commit_sha = self._head_sha(workdir)
            return _walk(workdir, source, self._caps, commit_sha=commit_sha)

    @staticmethod
    def _clone(source: GitSource, target: Path) -> None:
        # The trailing `--` terminates git's option parsing, so even if a
        # repo URL or path starts with `-` (Pydantic refuses this today
        # via the source schema) git won't interpret it as an option.
        cmd = [
            "git",
            "clone",
            "--depth=1",
            "--branch",
            source.branch,
            "--single-branch",
            "--no-tags",
            "--",
            _resolve_repo_url(source.repo),
            str(target),
        ]
        # cmd args come from a Pydantic-validated source spec; subprocess
        # input is not untrusted in the OWASP sense.
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone failed for source {source.name!r}: {proc.stderr.strip()}"
            )

    @staticmethod
    def _head_sha(workdir: Path) -> str:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(workdir), "rev-parse", "HEAD"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()


def _resolve_repo_url(repo: str) -> str:
    """Accept ``owner/name`` shorthand or full URL; passthrough URLs unchanged."""
    if "://" in repo or repo.startswith("file:") or repo.startswith("git@"):
        return repo
    return f"https://github.com/{repo}.git"


def fetch_source(source: Source, caps: IngestionCaps) -> FetchResult:
    """Dispatch on ``source.type`` and return the matching connector's result."""
    if isinstance(source, GitSource):
        return GitConnector(caps).fetch(source)
    if isinstance(source, LocalSource):
        return LocalConnector(caps).fetch(source)
    # Exhaustive: Pydantic schema rejects unknown types at load time.
    raise TypeError(f"unsupported source type: {type(source).__name__}")  # pragma: no cover


def _git_available() -> bool:  # pragma: no cover - convenience for callers
    return shutil.which("git") is not None
