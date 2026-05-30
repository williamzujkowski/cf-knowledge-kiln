"""#335 smoke test for the misses-vs-section-size cross-reference."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest


@pytest.fixture
def fake_diagnostic_csv(tmp_path: Path) -> Path:
    """A 3-doc CSV that triggers both branches of the verdict — one
    miss whose expected doc has a long section, and one whose
    expected doc only has short sections."""
    p = tmp_path / "diag.csv"
    rows = [
        # AGENTS.md has a long section → contributes to misses-with-long
        {
            "repo": "fixture",
            "path": "AGENTS.md",
            "heading_path": "Big section",
            "section_tokens": 1500,
            "n_chunks_produced": 2,
            "was_split": True,
        },
        {
            "repo": "fixture",
            "path": "AGENTS.md",
            "heading_path": "Small one",
            "section_tokens": 100,
            "n_chunks_produced": 1,
            "was_split": False,
        },
        # authentik-sso has only short sections → does NOT contribute
        {
            "repo": "fixture",
            "path": "docs/components/authentik-sso.md",
            "heading_path": "Intro",
            "section_tokens": 200,
            "n_chunks_produced": 1,
            "was_split": False,
        },
    ]
    with p.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "repo",
                "path",
                "heading_path",
                "section_tokens",
                "n_chunks_produced",
                "was_split",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return p


class TestChunkingMissesXref:
    def test_main_writes_report(self, fake_diagnostic_csv: Path, tmp_path: Path) -> None:
        from tests.eval.scripts.chunking_misses_xref import main

        out_dir = tmp_path / "reports"
        rc = main(["--csv", str(fake_diagnostic_csv), "--out", str(out_dir)])
        assert rc == 0
        md_files = list(out_dir.glob("chunking-misses-vs-section-size-*.md"))
        assert len(md_files) == 1

    def test_main_returns_2_on_missing_csv(self, tmp_path: Path) -> None:
        from tests.eval.scripts.chunking_misses_xref import main

        rc = main(["--csv", str(tmp_path / "nope.csv"), "--out", str(tmp_path)])
        assert rc == 2

    def test_report_includes_verdict_line(self, fake_diagnostic_csv: Path, tmp_path: Path) -> None:
        from tests.eval.scripts.chunking_misses_xref import main

        out_dir = tmp_path / "reports"
        main(["--csv", str(fake_diagnostic_csv), "--out", str(out_dir)])
        text = next(out_dir.glob("chunking-misses-vs-section-size-*.md")).read_text()
        assert "## Verdict" in text
        assert "/8 misses" in text  # the count is part of the contract

    def test_long_section_threshold_is_pinned(self) -> None:
        """The verdict bucketing depends on the 800-token threshold; pin it.
        If a future #339 PR widens the threshold, this test must be
        updated alongside the report's interpretation."""
        from tests.eval.scripts.chunking_misses_xref import LONG_SECTION_THRESHOLD

        assert LONG_SECTION_THRESHOLD == 800

    def test_path_matcher_handles_md_suffix(self) -> None:
        """The calibration table uses stems without ``.md``; the CSV
        carries full paths with ``.md``. The matcher must bridge."""
        from tests.eval.scripts.chunking_misses_xref import _path_matches

        # Bare AGENTS.md case.
        assert _path_matches("AGENTS.md", "AGENTS.md")
        # Stem-from-calibration → path-with-md in CSV.
        assert _path_matches("docs/components/authentik-sso.md", "docs/components/authentik-sso")
        # Subdir AGENTS.md.
        assert _path_matches("subproject/AGENTS.md", "AGENTS.md")
        # Non-match.
        assert not _path_matches("unrelated/doc.md", "AGENTS.md")
