"""Tests for git + local source connectors (#14).

Local-source tests use ``tmp_path`` fixtures. Git tests build a real
local bare repo via subprocess and point the connector at it through a
``file://`` URL, so no network is required.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cf_knowledge_kiln.ingestion.connectors import (
    GitConnector,
    IngestionCapExceeded,
    IngestionCaps,
    LocalConnector,
    fetch_source,
)
from cf_knowledge_kiln.ingestion.sources import GitSource, LocalSource

# ─── helpers ───────────────────────────────────────────────────────


def _make_caps(
    *,
    max_file_bytes: int = 1_048_576,
    max_files: int = 1_000,
    max_repo_bytes: int = 10 * 1_048_576,
) -> IngestionCaps:
    return IngestionCaps(
        max_file_bytes=max_file_bytes,
        max_files=max_files,
        max_repo_bytes=max_repo_bytes,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ─── LocalConnector ─────────────────────────────────────────────────


def test_local_connector_returns_matching_files(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "intro.md", "# Intro\nhello")
    _write(tmp_path / "docs" / "guide.md", "# Guide\nfoo")
    _write(tmp_path / "README.md", "# Root")
    src = LocalSource(name="example", type="local", path=str(tmp_path), include=["docs/**/*.md"])
    result = LocalConnector(_make_caps()).fetch(src)
    assert {f.path for f in result.files} == {"docs/intro.md", "docs/guide.md"}
    assert result.commit_sha is None


def test_local_connector_applies_exclude(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "intro.md", "x")
    _write(tmp_path / "docs" / "draft-wip.md", "x")
    src = LocalSource(
        name="x",
        type="local",
        path=str(tmp_path),
        include=["docs/**/*.md"],
        exclude=["**/draft-*.md"],
    )
    result = LocalConnector(_make_caps()).fetch(src)
    assert {f.path for f in result.files} == {"docs/intro.md"}
    reasons = {s.reason for s in result.skipped if s.path.endswith("draft-wip.md")}
    assert reasons == {"excluded_by_pattern"}


def test_local_connector_skips_non_markdown(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "intro.md", "x")
    _write(tmp_path / "docs" / "logo.png", "x")
    src = LocalSource(name="x", type="local", path=str(tmp_path), include=["docs/**/*"])
    result = LocalConnector(_make_caps()).fetch(src)
    assert {f.path for f in result.files} == {"docs/intro.md"}
    skipped = [s for s in result.skipped if s.path.endswith(".png")]
    assert skipped and skipped[0].reason == "unsupported_file_type"


def test_local_connector_skips_oversize_file(tmp_path: Path) -> None:
    _write(tmp_path / "small.md", "ok")
    _write(tmp_path / "big.md", "x" * 5000)
    src = LocalSource(name="x", type="local", path=str(tmp_path), include=["**/*.md"])
    result = LocalConnector(_make_caps(max_file_bytes=100)).fetch(src)
    assert {f.path for f in result.files} == {"small.md"}
    skipped = [s for s in result.skipped if s.path.endswith("big.md")]
    assert skipped and skipped[0].reason == "too_large"


def test_local_connector_aborts_on_total_size_cap(tmp_path: Path) -> None:
    for i in range(5):
        _write(tmp_path / f"f{i}.md", "x" * 2000)
    src = LocalSource(name="x", type="local", path=str(tmp_path), include=["**/*.md"])
    caps = _make_caps(max_repo_bytes=3000)
    with pytest.raises(IngestionCapExceeded, match="total size"):
        LocalConnector(caps).fetch(src)


def test_local_connector_aborts_on_file_count_cap(tmp_path: Path) -> None:
    for i in range(20):
        _write(tmp_path / f"f{i}.md", "x")
    src = LocalSource(name="x", type="local", path=str(tmp_path), include=["**/*.md"])
    caps = _make_caps(max_files=3)
    with pytest.raises(IngestionCapExceeded, match="file count"):
        LocalConnector(caps).fetch(src)


def test_local_connector_raises_on_missing_path(tmp_path: Path) -> None:
    src = LocalSource(name="x", type="local", path=str(tmp_path / "nope"), include=["**/*.md"])
    with pytest.raises(FileNotFoundError):
        LocalConnector(_make_caps()).fetch(src)


def test_local_connector_default_denies_dotfiles_and_vendored_trees(tmp_path: Path) -> None:
    """Dotfiles (`.env`, `.git/`, `node_modules/`) skipped regardless of include."""
    _write(tmp_path / ".env", "SECRET=value")
    _write(tmp_path / ".git" / "config", "noise")
    _write(tmp_path / "node_modules" / "x" / "README.md", "noise")
    _write(tmp_path / "docs" / ".env.local", "another-secret")
    _write(tmp_path / "docs" / "intro.md", "# Intro")
    src = LocalSource(name="x", type="local", path=str(tmp_path), include=["**/*"])
    result = LocalConnector(_make_caps()).fetch(src)
    assert {f.path for f in result.files} == {"docs/intro.md"}
    denied = {s.path for s in result.skipped if s.detail == "dotfile / vendored-tree default-deny"}
    assert ".env" in denied
    assert ".git/config" in denied
    assert "node_modules/x/README.md" in denied
    assert "docs/.env.local" in denied


# ─── GitConnector ───────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _git_available() -> bool:
    return shutil.which("git") is not None


@pytest.fixture
def local_git_repo(tmp_path_factory: pytest.TempPathFactory, _git_available: bool) -> Path:
    """Create a tiny bare repo + working clone with three markdown files."""
    if not _git_available:
        pytest.skip("git CLI not available")
    work = tmp_path_factory.mktemp("repo-work")
    bare = tmp_path_factory.mktemp("repo-bare")
    # Build the source repo.
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(
        ["git", "-C", str(work), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Tester"], check=True)
    (work / "README.md").write_text("# Root\n")
    (work / "docs").mkdir()
    (work / "docs" / "a.md").write_text("# A\n")
    (work / "docs" / "b.md").write_text("# B\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "init"],
        check=True,
        env={"GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"},
    )
    # Push to bare so the connector can clone from a stable URL.
    subprocess.run(["git", "-C", str(work), "clone", "--bare", str(work), str(bare)], check=True)
    return bare


def test_git_connector_clones_and_returns_files(local_git_repo: Path) -> None:
    src = GitSource(
        name="example",
        type="git",
        repo=f"file://{local_git_repo}",
        branch="main",
        include=["docs/**/*.md"],
    )
    result = GitConnector(_make_caps()).fetch(src)
    assert {f.path for f in result.files} == {"docs/a.md", "docs/b.md"}
    assert result.commit_sha is not None and len(result.commit_sha) == 40
    for f in result.files:
        assert f.commit_sha == result.commit_sha


def test_git_connector_respects_branch(local_git_repo: Path) -> None:
    src = GitSource(
        name="x",
        type="git",
        repo=f"file://{local_git_repo}",
        branch="main",
        include=["README.md"],
    )
    result = GitConnector(_make_caps()).fetch(src)
    assert {f.path for f in result.files} == {"README.md"}


def test_git_connector_raises_on_unknown_branch(local_git_repo: Path) -> None:
    src = GitSource(
        name="x",
        type="git",
        repo=f"file://{local_git_repo}",
        branch="does-not-exist",
        include=["**/*.md"],
    )
    with pytest.raises(RuntimeError, match="git"):
        GitConnector(_make_caps()).fetch(src)


def test_git_connector_aborts_on_repo_size_cap(local_git_repo: Path) -> None:
    src = GitSource(
        name="x",
        type="git",
        repo=f"file://{local_git_repo}",
        branch="main",
        include=["**/*.md"],
    )
    tiny_caps = _make_caps(max_repo_bytes=5)
    with pytest.raises(IngestionCapExceeded):
        GitConnector(tiny_caps).fetch(src)


# ─── dispatcher ────────────────────────────────────────────────────


def test_fetch_source_dispatches_on_type(tmp_path: Path) -> None:
    _write(tmp_path / "a.md", "x")
    src = LocalSource(name="x", type="local", path=str(tmp_path), include=["**/*.md"])
    result = fetch_source(src, _make_caps())
    assert {f.path for f in result.files} == {"a.md"}
