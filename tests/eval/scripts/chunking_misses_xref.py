#!/usr/bin/env python3
"""#335 — cross-reference the 8 calibration-222 misses against the
diagnostic CSV from :mod:`tests.eval.diagnose_chunking`.

For each miss, look up the expected document's longest section in
the diagnostic and report whether that longest section exceeded
800 tokens (the "long-section" threshold the issue specifies).

Run after :mod:`tests.eval.diagnose_chunking`. Output is a markdown
file in ``tests/eval/reports/`` answering the question:

    Are the 8 calibration-222 misses concentrated in long-section docs?

Source of truth for the misses list: the table in
``tests/eval/calibration-222-results-2026-05-24.md``. Hard-coded
here (8 rows) rather than parsed so a future report edit doesn't
silently change the misses set this analysis is comparing against.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# The 8 misses from calibration-222-results-2026-05-24.md. Each entry
# is (qid, list-of-expected-doc-path-stems). Paths use the
# heading-stripped form that matches the diagnostic CSV's ``path``
# column. Multi-target queries (q09, q11, q12) list every expected
# doc — the cross-reference reports each independently and ALSO an
# any-long-section verdict for the query.
MISSES: list[tuple[str, list[str]]] = [
    ("q01", ["skills/manage-offsite-backup"]),
    ("q04", ["docs/runbooks/offsitebackupfailed"]),
    ("q06", ["docs/components/offsite-backup"]),
    ("q08", ["AGENTS.md"]),
    ("q09", ["AGENTS.md", "scripts/wait-for-host"]),
    ("q10", ["skills/manage-offsite-backup"]),
    ("q11", ["AGENTS.md", "inventory/lab.yml"]),
    ("q12", ["docs/components/authentik-sso", "inventory/lab.yml"]),
]

# Per the issue: "long" = > 800 tokens. The chunker's default
# max_tokens is currently 1000; "long" here means "approaches the
# threshold" — useful proxy for sections the retriever might struggle
# with even before they split.
LONG_SECTION_THRESHOLD: int = 800


@dataclass
class DocStats:
    """Aggregate over all sections in one document."""

    n_sections: int = 0
    max_section_tokens: int = 0
    n_long: int = 0
    longest_heading: str = ""


def _path_matches(csv_path: str, expected_stem: str) -> bool:
    """Loose match — the calibration table uses path stems (no .md)
    while the CSV carries full relative paths. Match if the CSV path
    ends with ``<stem>.md`` OR equals ``<stem>``. AGENTS.md special-
    cased to also match a bare ``AGENTS.md`` at any depth."""
    return (
        csv_path == expected_stem
        or csv_path == expected_stem + ".md"
        or csv_path.endswith("/" + expected_stem + ".md")
        # AGENTS.md / inventory/lab.yml — bare-filename variants
        or csv_path.endswith("/" + expected_stem)
    )


def aggregate_doc_stats(csv_path: Path) -> dict[str, DocStats]:
    """Walk the diagnostic CSV and aggregate per-doc stats."""
    by_path: dict[str, DocStats] = defaultdict(DocStats)
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            path = row["path"]
            tokens = int(row["section_tokens"])
            stats = by_path[path]
            stats.n_sections += 1
            if tokens > LONG_SECTION_THRESHOLD:
                stats.n_long += 1
            if tokens > stats.max_section_tokens:
                stats.max_section_tokens = tokens
                stats.longest_heading = row["heading_path"] or "(top-level)"
    return dict(by_path)


def find_matches(doc_stats: dict[str, DocStats], expected_stem: str) -> list[tuple[str, DocStats]]:
    """All CSV paths that match the expected stem."""
    return [(p, s) for p, s in doc_stats.items() if _path_matches(p, expected_stem)]


def render_report(
    doc_stats: dict[str, DocStats],
    *,
    csv_path: Path,
    generated_at: datetime,
) -> str:
    """Produce the markdown summary."""
    lines: list[str] = []
    lines.append(f"# Calibration-222 misses vs section size — {generated_at.date().isoformat()}")
    lines.append("")
    lines.append(
        "Cross-reference of the 8 misses from "
        "`tests/eval/calibration-222-results-2026-05-24.md` against the "
        f"diagnostic CSV (`{csv_path.name}`). Section is 'long' if "
        f"`section_tokens > {LONG_SECTION_THRESHOLD}`."
    )
    lines.append("")
    lines.append("## Per-miss detail")
    lines.append("")
    lines.append(
        "| qid | expected | matched_path | max_section_tokens | "
        "longest_heading | n_long_sections | n_sections |"
    )
    lines.append("| --- | --- | --- | ---: | --- | ---: | ---: |")
    misses_with_any_long: int = 0
    for qid, expected_paths in MISSES:
        any_long = False
        for expected in expected_paths:
            matches = find_matches(doc_stats, expected)
            if not matches:
                lines.append(f"| {qid} | `{expected}` | _(no match in corpus)_ | — | — | — | — |")
                continue
            # If multiple matches (e.g., AGENTS.md exists in several
            # subdirs), emit one row per match — we want to see them all.
            for matched_path, stats in matches:
                lines.append(
                    f"| {qid} | `{expected}` | `{matched_path}` | "
                    f"{stats.max_section_tokens} | "
                    f"{stats.longest_heading} | {stats.n_long} | "
                    f"{stats.n_sections} |"
                )
                if stats.max_section_tokens > LONG_SECTION_THRESHOLD:
                    any_long = True
        if any_long:
            misses_with_any_long += 1
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    pct = (misses_with_any_long / len(MISSES)) if MISSES else 0.0
    lines.append(
        f"**{misses_with_any_long}/{len(MISSES)} misses** "
        f"({pct:.0%}) involve at least one expected doc whose longest "
        f"section exceeds {LONG_SECTION_THRESHOLD} tokens."
    )
    lines.append("")
    if misses_with_any_long >= max(1, len(MISSES) // 2):
        lines.append(
            "→ Misses ARE concentrated in long-section docs. This is "
            "consistent with the section-grouper producing sections "
            "that approach or exceed the embedding-window budget; the "
            "retriever's vector arm loses signal when a single section "
            "carries multiple sub-topics. Investigate "
            "header-anchored or recursive chunking (#339)."
        )
    else:
        lines.append(
            "→ Misses are NOT concentrated in long-section docs. The "
            "retrieval-quality concern is something other than section "
            "size — investigate query normalization, vector arm "
            "tuning, or boost weights before reaching for a new "
            "chunker (#339)."
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chunking_misses_xref",
        description="Cross-reference calibration-222 misses against the chunking diagnostic.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="Diagnostic CSV produced by tests.eval.diagnose_chunking.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/eval/reports"),
        help="Output directory (defaults to tests/eval/reports/).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not args.csv.exists():
        logger.error("CSV not found: %s — run tests.eval.diagnose_chunking first", args.csv)
        return 2
    doc_stats = aggregate_doc_stats(args.csv)
    generated_at = datetime.now(UTC)
    stamp = generated_at.date().isoformat()
    out = args.out / f"chunking-misses-vs-section-size-{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(doc_stats, csv_path=args.csv, generated_at=generated_at))
    logger.info("wrote cross-reference to %s", out)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
