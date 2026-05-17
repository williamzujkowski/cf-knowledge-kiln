"""Tests for the source allowlist loader and Pydantic schema (#13)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cf_knowledge_kiln.ingestion.sources import (
    GitSource,
    LocalSource,
    SourceAllowlist,
    SourceAllowlistError,
    SourceNotAllowedError,
)


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(yaml.safe_dump(payload))
    return p


# ─── schema ────────────────────────────────────────────────────────


def test_git_source_minimum_fields_validate() -> None:
    s = GitSource(name="example", type="git", repo="owner/name")
    assert s.branch == "main"
    assert s.include == []
    assert s.status == "active"
    assert s.default_sensitivity == "internal"


def test_git_source_with_full_fields() -> None:
    s = GitSource(
        name="internal-docs",
        type="git",
        repo="org/internal-docs",
        branch="develop",
        include=["docs/**/*.md"],
        exclude=["**/draft-*.md"],
        status="active",
        authority="reference",
        default_owner="platform",
        default_sensitivity="restricted",
        last_reviewed_required=True,
    )
    assert s.authority == "reference"
    assert s.last_reviewed_required is True


def test_local_source_with_path() -> None:
    s = LocalSource(name="vendor-docs", type="local", path="/srv/docs/vendor")
    assert s.path == "/srv/docs/vendor"
    assert s.status == "active"


def test_git_source_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        GitSource(name="n", type="git", repo="o/r", spurious_field=1)  # type: ignore[call-arg]


def test_invalid_status_rejected() -> None:
    with pytest.raises(ValueError):
        GitSource(name="n", type="git", repo="o/r", status="purple")  # type: ignore[arg-type]


def test_invalid_sensitivity_rejected() -> None:
    with pytest.raises(ValueError):
        GitSource(
            name="n",
            type="git",
            repo="o/r",
            default_sensitivity="opaque",  # type: ignore[arg-type]
        )


# ─── loader ────────────────────────────────────────────────────────


def test_loader_parses_minimal_git_source(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": [{"name": "x", "type": "git", "repo": "o/r"}]})
    allowlist = SourceAllowlist.from_yaml(p)
    assert len(allowlist) == 1
    src = allowlist.get("x")
    assert isinstance(src, GitSource)
    assert src.repo == "o/r"


def test_loader_parses_mixed_git_and_local(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "sources": [
                {"name": "a", "type": "git", "repo": "o/r"},
                {"name": "b", "type": "local", "path": "/srv/docs"},
            ]
        },
    )
    allowlist = SourceAllowlist.from_yaml(p)
    assert len(allowlist) == 2
    assert isinstance(allowlist.get("a"), GitSource)
    assert isinstance(allowlist.get("b"), LocalSource)


def test_loader_refuses_unknown_source(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": [{"name": "x", "type": "git", "repo": "o/r"}]})
    allowlist = SourceAllowlist.from_yaml(p)
    with pytest.raises(SourceNotAllowedError, match="not-in-allowlist"):
        allowlist.get("not-in-allowlist")


def test_loader_rejects_duplicate_names(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "sources": [
                {"name": "x", "type": "git", "repo": "o/r"},
                {"name": "x", "type": "local", "path": "/tmp"},  # noqa: S108
            ]
        },
    )
    with pytest.raises(SourceAllowlistError, match="duplicate"):
        SourceAllowlist.from_yaml(p)


def test_loader_rejects_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "sources.yaml"
    p.write_text(":\n  - not: valid: yaml")
    with pytest.raises(SourceAllowlistError, match="YAML"):
        SourceAllowlist.from_yaml(p)


def test_loader_rejects_missing_sources_key(tmp_path: Path) -> None:
    p = _write(tmp_path, {"other_key": []})
    with pytest.raises(SourceAllowlistError, match="sources"):
        SourceAllowlist.from_yaml(p)


def test_loader_rejects_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "does-not-exist.yaml"
    with pytest.raises(SourceAllowlistError, match="not found"):
        SourceAllowlist.from_yaml(p)


def test_loader_rejects_invalid_source_type(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": [{"name": "x", "type": "http", "url": "https://e"}]})
    with pytest.raises(SourceAllowlistError):
        SourceAllowlist.from_yaml(p)


def test_loader_allows_empty_source_list(tmp_path: Path) -> None:
    p = _write(tmp_path, {"sources": []})
    allowlist = SourceAllowlist.from_yaml(p)
    assert len(allowlist) == 0


def test_loader_iterates_in_declaration_order(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "sources": [
                {"name": "a", "type": "git", "repo": "o/a"},
                {"name": "b", "type": "git", "repo": "o/b"},
                {"name": "c", "type": "git", "repo": "o/c"},
            ]
        },
    )
    allowlist = SourceAllowlist.from_yaml(p)
    assert [s.name for s in allowlist] == ["a", "b", "c"]


def test_loader_skips_inactive_via_active_property(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "sources": [
                {"name": "live", "type": "git", "repo": "o/r"},
                {"name": "paused", "type": "git", "repo": "o/r2", "status": "inactive"},
            ]
        },
    )
    allowlist = SourceAllowlist.from_yaml(p)
    active = list(allowlist.active())
    assert {s.name for s in active} == {"live"}


# ─── example file in repo ──────────────────────────────────────────


def test_example_yaml_parses() -> None:
    """The shipped config/sources.example.yaml must round-trip through the schema."""
    example = Path(__file__).resolve().parents[2] / "config" / "sources.example.yaml"
    allowlist = SourceAllowlist.from_yaml(example)
    assert len(allowlist) >= 1
    for src in allowlist:
        assert src.name
