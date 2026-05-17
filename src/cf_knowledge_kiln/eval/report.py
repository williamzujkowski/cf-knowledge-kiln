"""Render :class:`EvalReport` to JSON + Markdown.

JSON is the CI-consumable representation; Markdown is the human one
(paste into a PR description). Both shapes are deliberately
schema-stable so we can diff between runs without parsing surprises.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cf_knowledge_kiln.eval.scoring import EvalReport


def _now_utc() -> datetime:
    return datetime.now(UTC)


def to_json_dict(report: EvalReport, *, generated_at: datetime | None = None) -> dict[str, Any]:
    """Stable JSON representation of an :class:`EvalReport`."""
    return {
        "generated_at": (generated_at or _now_utc()).isoformat(),
        "case_count": report.aggregate.case_count,
        "k_values": list(report.k_values),
        "aggregate": {
            "mrr": round(report.aggregate.mrr, 4),
            "recall_at": {str(k): round(v, 4) for k, v in report.aggregate.recall_at.items()},
        },
        "per_case": [
            {
                "case_id": c.case_id,
                "mrr": round(c.mrr, 4),
                "recall_at": {str(k): round(v, 4) for k, v in c.recall_at.items()},
                "per_hit_ranks": list(c.per_hit_ranks),
                "first_miss": asdict(c.first_miss) if c.first_miss else None,
            }
            for c in report.per_case
        ],
    }


def write_json(report: EvalReport, path: Path) -> None:
    """Write the report to ``path`` as pretty-printed JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_json_dict(report), indent=2) + "\n", encoding="utf-8")


def to_markdown(report: EvalReport, *, generated_at: datetime | None = None) -> str:
    """Human-readable summary table of an :class:`EvalReport`."""
    ts = (generated_at or _now_utc()).isoformat()
    lines = [
        "# Retrieval eval report",
        "",
        f"_Generated {ts} — {report.aggregate.case_count} cases_",
        "",
        "## Aggregate",
        "",
        f"- **MRR:** {report.aggregate.mrr:.3f}",
    ]
    for k in report.k_values:
        v = report.aggregate.recall_at.get(k, 0.0)
        lines.append(f"- **Recall@{k}:** {v:.3f}")
    lines += ["", "## Per case", ""]
    header = "| case | MRR | " + " | ".join(f"R@{k}" for k in report.k_values) + " | first miss |"
    sep = "|---|---|" + "|".join(["---"] * len(report.k_values)) + "|---|"
    lines.append(header)
    lines.append(sep)
    for c in report.per_case:
        recalls = " | ".join(f"{c.recall_at.get(k, 0.0):.2f}" for k in report.k_values)
        miss = "-"
        if c.first_miss is not None:
            miss_heading = "/".join(c.first_miss.heading_path) or "(doc)"
            miss = f"`{c.first_miss.path}` § {miss_heading}"
        lines.append(f"| {c.case_id} | {c.mrr:.2f} | {recalls} | {miss} |")
    return "\n".join(lines) + "\n"


def write_markdown(report: EvalReport, path: Path) -> None:
    """Write the report to ``path`` as Markdown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(report), encoding="utf-8")


__all__ = [
    "to_json_dict",
    "to_markdown",
    "write_json",
    "write_markdown",
]
