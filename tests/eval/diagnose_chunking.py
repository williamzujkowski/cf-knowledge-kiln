#!/usr/bin/env python3
"""#335 — chunking diagnostic CLI.

Walks each configured ``local`` source via the ingestion pipeline,
parses every Markdown doc, and emits per-section statistics so the
chunking ADR (#344) has empirical evidence to ground "is the
section-based chunker producing too-large sections that hurt
retrieval?"

Outputs two files under ``tests/eval/reports/``:

* ``chunking-distribution-YYYY-MM-DD.csv`` — one row per section:
  ``repo, path, heading_path, section_tokens, n_chunks_produced,
  was_split``.
* ``chunking-distribution-YYYY-MM-DD.md`` — human-readable summary:
  per-repo p50 / p95 / max section size, top-20 longest sections
  per repo, fraction of sections that were split, distribution of
  chunks-per-section.

Read-only investigation tool — no production code is touched. The
script imports the private ``_scan_blocks`` / ``_group_into_sections``
/ ``_pack_blocks`` helpers from :mod:`cf_knowledge_kiln.ingestion.chunking`
so it observes exactly the same section boundaries the production
parser uses; if those private helpers ever go public, swap the
imports.

Usage::

    python -m tests.eval.diagnose_chunking \\
        --config config/sources.local.yaml \\
        --out tests/eval/reports/

Per the issue the script targets ``local`` sources only. Git +
HTTP sources are skipped with a warning (no clone / fetch — this
script is offline + deterministic).
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from cf_knowledge_kiln.ingestion.chunking import (
    DEFAULT_MAX_TOKENS,
    _group_into_sections,
    _pack_blocks,
    _scan_blocks,
    _section_text,
    count_tokens,
)
from cf_knowledge_kiln.ingestion.sources import LocalSource, SourceAllowlist

logger = logging.getLogger(__name__)

CSV_COLUMNS = (
    "repo",
    "path",
    "heading_path",
    "section_tokens",
    "n_chunks_produced",
    "was_split",
)


@dataclass(frozen=True)
class SectionRow:
    """One row in the diagnostic CSV. Matches :data:`CSV_COLUMNS` order."""

    repo: str
    path: str
    heading_path: str
    section_tokens: int
    n_chunks_produced: int
    was_split: bool

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "repo": self.repo,
            "path": self.path,
            "heading_path": self.heading_path,
            "section_tokens": self.section_tokens,
            "n_chunks_produced": self.n_chunks_produced,
            "was_split": self.was_split,
        }


def iter_section_diagnostics(
    repo: str,
    path: str,
    source_text: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Iterator[SectionRow]:
    """Yield per-section diagnostic rows for one Markdown document.

    Mirrors the production parser's section grouping exactly — the
    diagnostic numbers and the ingestion numbers cannot drift.
    """
    try:
        fm = frontmatter.loads(source_text)
        body = fm.content
    except Exception:
        # Malformed frontmatter → no sections (the ingestion parser
        # records this as a parse_error; the diagnostic skips silently
        # because the CSV would carry no useful row).
        return
    blocks = _scan_blocks(body)
    sections = _group_into_sections(blocks)
    for section in sections:
        text = _section_text(section)
        if not text:
            continue
        section_tokens = count_tokens(text)
        if section_tokens <= max_tokens:
            n_chunks = 1
        else:
            n_chunks = sum(1 for _ in _pack_blocks(section.blocks, max_tokens))
        yield SectionRow(
            repo=repo,
            path=path,
            heading_path=" > ".join(section.heading_path),
            section_tokens=section_tokens,
            n_chunks_produced=n_chunks,
            was_split=n_chunks > 1,
        )


def walk_local_source(
    source: LocalSource,
) -> Iterator[tuple[str, Path]]:
    """Yield (repo_name, file_path) for every Markdown file under a local source.

    ``include`` glob patterns from the source spec are applied; if
    none are configured, defaults to ``**/*.md``. Excludes hidden
    directories (``.git``, ``.venv``, ``node_modules``) by simple
    prefix check — production ingestion has a more elaborate
    skip-list but the diagnostic doesn't need it (the false-positive
    rate is negligible against typical doc corpora).
    """
    root = Path(source.path).expanduser().resolve()
    if not root.exists():
        logger.warning("local source %r path does not exist: %s", source.name, root)
        return
    patterns: list[str] = list(getattr(source, "include", None) or ["**/*.md"])
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path in seen:
                continue
            if not path.is_file():
                continue
            # Cheap skip-list. Production ingestion uses a full
            # exclusion engine but for offline diagnostic this is
            # sufficient.
            parts = path.relative_to(root).parts
            if any(p.startswith(".") or p in ("node_modules", "__pycache__") for p in parts):
                continue
            seen.add(path)
            yield source.name, path


def collect_rows(allowlist: SourceAllowlist, *, max_tokens: int) -> list[SectionRow]:
    """Walk every local source in the allowlist and collect per-section rows.

    Non-local sources (git, http) are skipped with a warning.
    """
    rows: list[SectionRow] = []
    for source in allowlist:
        if not isinstance(source, LocalSource):
            logger.info(
                "skipping non-local source %r (type=%s); diagnostic is offline-only",
                source.name,
                getattr(source, "type", "?"),
            )
            continue
        for repo_name, path in walk_local_source(source):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("could not read %s: %s", path, exc)
                continue
            rel = str(path.relative_to(Path(source.path).expanduser().resolve()))
            rows.extend(iter_section_diagnostics(repo_name, rel, text, max_tokens=max_tokens))
    return rows


def write_csv(rows: list[SectionRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())


def _percentile(values: list[int], p: float) -> int:
    """Inclusive linear-interpolated percentile. Returns 0 on empty input."""
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return int(sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f))


def _per_repo_summary(rows: list[SectionRow]) -> dict[str, dict[str, int | float]]:
    """Compute p50/p95/max section size, split-fraction, and chunk-distribution per repo."""
    by_repo: dict[str, list[SectionRow]] = {}
    for row in rows:
        by_repo.setdefault(row.repo, []).append(row)
    summary: dict[str, dict[str, int | float]] = {}
    for repo, repo_rows in by_repo.items():
        tokens = [r.section_tokens for r in repo_rows]
        n_split = sum(1 for r in repo_rows if r.was_split)
        chunks_dist = [r.n_chunks_produced for r in repo_rows]
        summary[repo] = {
            "n_sections": len(repo_rows),
            "n_split": n_split,
            "split_fraction": round(n_split / len(repo_rows), 4) if repo_rows else 0.0,
            "p50_tokens": _percentile(tokens, 0.5),
            "p95_tokens": _percentile(tokens, 0.95),
            "max_tokens": max(tokens) if tokens else 0,
            "mean_chunks_per_section": (
                round(statistics.mean(chunks_dist), 2) if chunks_dist else 0.0
            ),
            "max_chunks_per_section": max(chunks_dist) if chunks_dist else 0,
        }
    return summary


def _top_n_longest(rows: list[SectionRow], n: int = 20) -> dict[str, list[SectionRow]]:
    """Per-repo top-N longest sections."""
    by_repo: dict[str, list[SectionRow]] = {}
    for row in rows:
        by_repo.setdefault(row.repo, []).append(row)
    return {
        repo: sorted(rs, key=lambda r: r.section_tokens, reverse=True)[:n]
        for repo, rs in by_repo.items()
    }


def write_markdown_summary(
    rows: list[SectionRow],
    *,
    out_path: Path,
    max_tokens: int,
    generated_at: datetime,
) -> None:
    summary = _per_repo_summary(rows)
    longest = _top_n_longest(rows, n=20)
    lines: list[str] = []
    lines.append(f"# Chunking diagnostic — {generated_at.date().isoformat()}")
    lines.append("")
    lines.append(
        f"Section-size distribution across the local corpus(es) parsed by "
        f"`cf_knowledge_kiln.ingestion.chunking.parse_document`. Each row in "
        f"the companion CSV is one section; sections that exceed "
        f"`max_tokens={max_tokens}` are split into multiple chunks at parse "
        f"time."
    )
    lines.append("")
    lines.append(f"- Total sections observed: **{len(rows)}**")
    lines.append(f"- Generated at: {generated_at.isoformat()}")
    lines.append("")
    lines.append("## Per-repo summary")
    lines.append("")
    lines.append(
        "| repo | n_sections | p50 | p95 | max | split_frac | "
        "mean chunks/section | max chunks/section |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for repo in sorted(summary):
        s = summary[repo]
        lines.append(
            f"| {repo} | {s['n_sections']} | {s['p50_tokens']} | "
            f"{s['p95_tokens']} | {s['max_tokens']} | "
            f"{s['split_fraction']:.2%} | {s['mean_chunks_per_section']} | "
            f"{s['max_chunks_per_section']} |"
        )
    lines.append("")
    lines.append("## Top-20 longest sections per repo")
    lines.append("")
    for repo in sorted(longest):
        lines.append(f"### {repo}")
        lines.append("")
        lines.append("| path | heading_path | section_tokens | n_chunks |")
        lines.append("| --- | --- | ---: | ---: |")
        for row in longest[repo]:
            heading = row.heading_path or "(top-level)"
            lines.append(
                f"| `{row.path}` | {heading} | {row.section_tokens} | {row.n_chunks_produced} |"
            )
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diagnose_chunking",
        description="Emit per-section chunking statistics for the ADR.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/sources.local.yaml"),
        help="Source allowlist YAML (defaults to config/sources.local.yaml).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/eval/reports"),
        help="Output directory (defaults to tests/eval/reports/).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Section-size threshold (defaults to {DEFAULT_MAX_TOKENS}).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not args.config.exists():
        logger.error("config file not found: %s", args.config)
        return 2
    allowlist = SourceAllowlist.from_yaml(args.config)
    rows = collect_rows(allowlist, max_tokens=args.max_tokens)
    if not rows:
        logger.warning(
            "no sections collected — check that the configured local sources contain Markdown."
        )
    generated_at = datetime.now(UTC)
    stamp = generated_at.date().isoformat()
    csv_out = args.out / f"chunking-distribution-{stamp}.csv"
    md_out = args.out / f"chunking-distribution-{stamp}.md"
    write_csv(rows, csv_out)
    write_markdown_summary(
        rows, out_path=md_out, max_tokens=args.max_tokens, generated_at=generated_at
    )
    logger.info("wrote %d section rows to %s", len(rows), csv_out)
    logger.info("wrote summary to %s", md_out)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
