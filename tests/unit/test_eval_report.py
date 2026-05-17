"""Unit tests for eval report serialization (#31)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from cf_knowledge_kiln.eval.dataset import ExpectedHit
from cf_knowledge_kiln.eval.report import (
    to_json_dict,
    to_markdown,
    write_json,
    write_markdown,
)
from cf_knowledge_kiln.eval.scoring import (
    AggregateMetrics,
    CaseResult,
    EvalReport,
)


def _report() -> EvalReport:
    per_case = [
        CaseResult(
            case_id="c1",
            mrr=1.0,
            recall_at={1: 1.0, 3: 1.0, 5: 1.0, 10: 1.0},
            per_hit_ranks=[0],
        ),
        CaseResult(
            case_id="c2",
            mrr=0.5,
            recall_at={1: 0.0, 3: 1.0, 5: 1.0, 10: 1.0},
            per_hit_ranks=[1, None],
            first_miss=ExpectedHit(repo="r", path="never.md"),
        ),
    ]
    agg = AggregateMetrics(
        mrr=0.75,
        recall_at={1: 0.5, 3: 1.0, 5: 1.0, 10: 1.0},
        case_count=2,
    )
    return EvalReport(per_case=per_case, aggregate=agg, k_values=(1, 3, 5, 10))


def test_to_json_dict_shape() -> None:
    out = to_json_dict(_report(), generated_at=datetime(2026, 5, 17, 12, 0, 0))
    assert out["case_count"] == 2
    assert out["k_values"] == [1, 3, 5, 10]
    assert out["aggregate"]["mrr"] == 0.75
    assert out["aggregate"]["recall_at"]["10"] == 1.0
    assert out["per_case"][1]["first_miss"]["path"] == "never.md"
    assert out["per_case"][1]["per_hit_ranks"] == [1, None]


def test_write_json_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "latest.json"
    write_json(_report(), path)
    parsed = json.loads(path.read_text())
    assert parsed["case_count"] == 2


def test_to_markdown_renders_aggregate_and_per_case() -> None:
    out = to_markdown(_report(), generated_at=datetime(2026, 5, 17, 12, 0, 0))
    assert "Retrieval eval report" in out
    assert "Recall@10:** 1.000" in out
    assert "| c1 |" in out
    assert "| c2 |" in out
    # The miss should render with path + (doc) heading shorthand.
    assert "`never.md` § (doc)" in out


def test_write_markdown_writes_file(tmp_path: Path) -> None:
    path = tmp_path / "out.md"
    write_markdown(_report(), path)
    body = path.read_text()
    assert body.startswith("# Retrieval eval report")
