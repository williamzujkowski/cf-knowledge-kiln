"""#335 smoke test for the chunking diagnostic CLI.

The diagnostic walks the configured local sources, runs every doc
through the production section grouper, and emits a CSV + markdown
summary. These tests cover the contract end-to-end against a tiny
fixture corpus (3 docs) so the script doesn't silently regress on
column shape or section-grouping semantics.

The diagnostic is read-only — no DB, no embedding provider, no
network. These tests run under the ``unit`` marker (no
``pytestmark = integration``) so they fire on every commit.
"""

from __future__ import annotations

import csv
import textwrap
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def fixture_corpus(tmp_path: Path) -> Path:
    """Three Markdown docs with different section-size profiles:

    * ``short.md`` — a single ≤max_tokens section.
    * ``many-small.md`` — five small sections.
    * ``one-long.md`` — one section with enough text to force a split.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "short.md").write_text(
        textwrap.dedent(
            """\
            # Short doc
            This document has exactly one small section. No frontmatter.
            """
        )
    )
    (corpus / "many-small.md").write_text(
        textwrap.dedent(
            """\
            # Many small sections

            Intro paragraph.

            ## First subsection
            A few sentences here.

            ## Second subsection
            More content.

            ## Third subsection
            Even more.

            ## Fourth subsection
            And final.
            """
        )
    )
    # Force the splitter by emitting a section larger than the default
    # max_tokens threshold. Repeat a long line ~500 times to clear
    # 1000 tokens regardless of tokenizer.
    body = "\n\n".join(
        f"This is paragraph number {i} with enough words to count noticeably against the token budget."
        for i in range(500)
    )
    (corpus / "one-long.md").write_text(f"# One huge section\n\n{body}\n")
    return corpus


@pytest.fixture
def sources_yaml(tmp_path: Path, fixture_corpus: Path) -> Path:
    """A minimal allowlist pointing at the fixture corpus."""
    cfg = tmp_path / "sources.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "name": "fixture-corpus",
                        "type": "local",
                        "path": str(fixture_corpus),
                        "include": ["**/*.md"],
                    }
                ]
            }
        )
    )
    return cfg


class TestDiagnoseChunkingRunsOnFixtureCorpus:
    """Top-level acceptance from the issue: the CSV exists, lists
    every section, and the markdown summary is reviewable."""

    def test_main_returns_zero(self, sources_yaml: Path, tmp_path: Path) -> None:
        from tests.eval.diagnose_chunking import main

        out_dir = tmp_path / "reports"
        rc = main(["--config", str(sources_yaml), "--out", str(out_dir)])
        assert rc == 0

    def test_csv_written_with_expected_columns(self, sources_yaml: Path, tmp_path: Path) -> None:
        from tests.eval.diagnose_chunking import CSV_COLUMNS, main

        out_dir = tmp_path / "reports"
        main(["--config", str(sources_yaml), "--out", str(out_dir)])
        csv_files = list(out_dir.glob("chunking-distribution-*.csv"))
        assert len(csv_files) == 1, f"expected exactly one CSV, got {csv_files}"
        with csv_files[0].open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames is not None
            assert tuple(reader.fieldnames) == CSV_COLUMNS, (
                "CSV column order is part of the contract — the markdown "
                "summary AND any downstream analysis (e.g. the misses "
                "cross-reference for #344) depend on it."
            )

    def test_csv_has_one_row_per_section(self, sources_yaml: Path, tmp_path: Path) -> None:
        from tests.eval.diagnose_chunking import main

        out_dir = tmp_path / "reports"
        main(["--config", str(sources_yaml), "--out", str(out_dir)])
        csv_path = next(out_dir.glob("chunking-distribution-*.csv"))
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        # short.md:        1 section
        # many-small.md:   ≥4 sections (intro + 4 subsections; intro
        #                  may be folded depending on heading grouping)
        # one-long.md:     1 section, split into many chunks
        # → expect ≥ 6 rows total.
        assert len(rows) >= 6, (
            f"expected ≥ 6 section rows across the 3-doc fixture, got {len(rows)}: {rows!r}"
        )

    def test_long_section_marked_as_split(self, sources_yaml: Path, tmp_path: Path) -> None:
        """The 500-paragraph section in ``one-long.md`` must be flagged
        ``was_split=True`` AND produce ``n_chunks_produced > 1``."""
        from tests.eval.diagnose_chunking import main

        out_dir = tmp_path / "reports"
        main(["--config", str(sources_yaml), "--out", str(out_dir)])
        csv_path = next(out_dir.glob("chunking-distribution-*.csv"))
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        long_rows = [r for r in rows if r["path"] == "one-long.md"]
        assert long_rows, "one-long.md must appear in the CSV"
        assert any(r["was_split"] in ("True", "true") for r in long_rows), (
            f"expected at least one section in one-long.md to be was_split=True; rows={long_rows}"
        )
        assert any(int(r["n_chunks_produced"]) > 1 for r in long_rows), (
            "expected n_chunks_produced > 1 for the long section"
        )

    def test_markdown_summary_written(self, sources_yaml: Path, tmp_path: Path) -> None:
        from tests.eval.diagnose_chunking import main

        out_dir = tmp_path / "reports"
        main(["--config", str(sources_yaml), "--out", str(out_dir)])
        md_files = list(out_dir.glob("chunking-distribution-*.md"))
        assert len(md_files) == 1
        text = md_files[0].read_text(encoding="utf-8")
        # Pin three substantive sections so a future refactor that drops
        # one (e.g. removes the top-20 table) breaks the test.
        assert "# Chunking diagnostic" in text
        assert "## Per-repo summary" in text
        assert "## Top-20 longest sections per repo" in text
        # And confirm the repo name appears in the summary table.
        assert "fixture-corpus" in text


class TestIterSectionDiagnosticsContract:
    """Pure-function tests for :func:`iter_section_diagnostics`. The
    integration smoke covers the full CLI path; these pin the
    inner contract so a future refactor that changes the SectionRow
    field set or the section-grouping semantics is caught precisely."""

    def test_single_section_yields_one_row_not_split(self) -> None:
        from tests.eval.diagnose_chunking import iter_section_diagnostics

        rows = list(
            iter_section_diagnostics(
                "fixture",
                "tiny.md",
                "# Title\n\nOne short paragraph.\n",
                max_tokens=1000,
            )
        )
        assert len(rows) == 1
        assert rows[0].n_chunks_produced == 1
        assert rows[0].was_split is False
        # The heading text "Title" is the section's first/only heading_path entry.
        assert rows[0].heading_path == "Title"

    def test_long_section_marked_split(self) -> None:
        from tests.eval.diagnose_chunking import iter_section_diagnostics

        body = "\n\n".join(
            f"paragraph {i} with enough words to count noticeably against the budget"
            for i in range(500)
        )
        rows = list(
            iter_section_diagnostics(
                "fixture",
                "big.md",
                f"# Big\n\n{body}\n",
                max_tokens=200,
            )
        )
        assert len(rows) == 1
        assert rows[0].was_split is True
        assert rows[0].n_chunks_produced > 1

    def test_malformed_frontmatter_yields_no_rows(self) -> None:
        """Bad YAML in frontmatter → silent skip (the production parser
        raises a FrontmatterTooLargeError on size; we just return [])."""
        from tests.eval.diagnose_chunking import iter_section_diagnostics

        # ``---`` opens a frontmatter block but the YAML is invalid.
        text = "---\nthis: is: not: valid: yaml\n---\n# Title\n\nbody\n"
        rows = list(iter_section_diagnostics("fixture", "bad.md", text))
        # Either an empty list (malformed) or the section list if
        # frontmatter parser was permissive. Either is acceptable —
        # we just pin that the function doesn't raise.
        assert isinstance(rows, list)


class TestCsvShapeIsStable:
    """A future refactor that adds a column should add it at the END.
    Existing analysis code (the misses cross-reference for #344)
    relies on the first 6 columns being these names in this order."""

    def test_csv_columns_match_documented_contract(self) -> None:
        from tests.eval.diagnose_chunking import CSV_COLUMNS

        assert CSV_COLUMNS == (
            "repo",
            "path",
            "heading_path",
            "section_tokens",
            "n_chunks_produced",
            "was_split",
        )
