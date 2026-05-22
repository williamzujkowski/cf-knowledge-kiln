"""Reproducible re-grading harness for the review-precision relevance map (#165).

``tests/eval/golden/review_precision.yaml`` carries per-chunk relevance
grades (0-3) — the median of several independent judges scoring the
retriever's actual top-3 chunks per case. That grading was a one-off
manual fan-out; this script makes it reproducible.

Two subcommands:

* ``worksheet`` — seed ``docs/_eval/`` with the real embedding model,
  run the retriever over every review case, and emit the
  (case, query, top-3 chunk) pairs that need grading — with the rubric
  — to ``tests/eval/reports/grading_worksheet.yaml``. Deterministic
  given the corpus + model.
* ``aggregate`` — given N per-judge grade files, compute the per-pair
  median grade + consensus stats and print the ``relevance:`` blocks
  ready to paste into ``review_precision.yaml``. Deterministic.

The only non-deterministic step — a judge assigning a grade — is left
to graders (independent subagents or humans); the worksheet structures
their input and the per-judge files are committable artifacts. See
``tests/eval/golden/GRADING.md`` for the full procedure.

This is a maintenance script, not a test — pytest does not collect it.
Run it directly: ``python -m tests.eval.regrade_review_precision ...``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_SET = _REPO_ROOT / "tests" / "eval" / "golden" / "review_precision.yaml"
_EVAL_CORPUS_DIR = _REPO_ROOT / "docs" / "_eval"
_WORKSHEET_OUT = _REPO_ROOT / "tests" / "eval" / "reports" / "grading_worksheet.yaml"
_SECURITY_CONFIG = _REPO_ROOT / "config" / "security.example.yaml"

RUBRIC: dict[int, str] = {
    3: "perfect answer to the query",
    2: "useful, partial",
    1: "tangentially relevant",
    0: "irrelevant",
}

_TRUNCATE_TABLES = (
    "rag_feedback, rag_queries, context_packs, chunk_embeddings, "
    "document_chunks, ingestion_runs, ingestion_jobs, documents, "
    "data_sources, model_registry"
)


# ─── aggregate: median + consensus (pure, deterministic, unit-tested) ──


@dataclass(frozen=True)
class PairConsensus:
    """Consensus over one (case, citation) pair across all judges."""

    case_id: str
    citation: str
    grades: list[int]
    median: int
    spread: int  # max - min; 0 == unanimous


def aggregate_grades(
    judge_grades: Sequence[Mapping[str, Mapping[str, int]]],
) -> list[PairConsensus]:
    """Median + consensus per (case_id, citation) across N judge maps.

    Each judge map is ``{case_id: {citation: grade}}``. A pair graded by
    only some judges still aggregates over the judges that graded it.
    The median is rounded to an integer grade; ``spread`` is max - min
    (0 means unanimous).
    """
    pairs: dict[tuple[str, str], list[int]] = {}
    for judge in judge_grades:
        for case_id, grades in judge.items():
            for citation, grade in grades.items():
                pairs.setdefault((case_id, citation), []).append(int(grade))
    out: list[PairConsensus] = []
    for (case_id, citation), grades in sorted(pairs.items()):
        out.append(
            PairConsensus(
                case_id=case_id,
                citation=citation,
                grades=grades,
                median=round(statistics.median(grades)),
                spread=max(grades) - min(grades),
            )
        )
    return out


def _consensus_summary(pairs: Sequence[PairConsensus]) -> str:
    """One-line consensus health summary, mirroring the YAML header note."""
    total = len(pairs)
    unanimous = sum(1 for p in pairs if p.spread == 0)
    wide = sum(1 for p in pairs if p.spread >= 2)
    return f"{unanimous}/{total} unanimous, {wide}/{total} with disagreement spread >= 2"


def _render_relevance_blocks(pairs: Sequence[PairConsensus]) -> str:
    """Render the aggregated grades as ``relevance:`` YAML blocks."""
    by_case: dict[str, list[PairConsensus]] = {}
    for p in pairs:
        by_case.setdefault(p.case_id, []).append(p)
    lines: list[str] = [f"# consensus: {_consensus_summary(pairs)}", ""]
    for case_id, case_pairs in by_case.items():
        lines.append(f"{case_id}:")
        lines.append("  relevance:")
        for p in case_pairs:
            lines.append(f'    "{p.citation}": {p.median}  # judges={p.grades}')
        lines.append("")
    return "\n".join(lines)


# ─── worksheet: run the real retriever, emit pairs needing grades ──────


async def _build_worksheet(database_url: str) -> dict[str, Any]:
    """Seed docs/_eval/, run context_pack per case, collect top-3 chunks."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from cf_knowledge_kiln.config import Settings
    from cf_knowledge_kiln.db.connection import Database
    from cf_knowledge_kiln.eval import load_review_set
    from cf_knowledge_kiln.ingestion.pipeline import run_source
    from cf_knowledge_kiln.ingestion.prompt_injection import load_phrases
    from cf_knowledge_kiln.ingestion.sensitive_content import load_patterns
    from cf_knowledge_kiln.ingestion.sources import LocalSource
    from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters, load_retrieval_config

    # Real embeddings — the worksheet must reflect the production top-3.
    os.environ["KILN_EVAL_REAL_EMBEDDINGS"] = "1"
    from tests.eval._review_precision_helpers import _build_embedding_provider, _citation_key

    settings = Settings(_env_file=None)
    cases = load_review_set(_REVIEW_SET)
    phrases = load_phrases(_SECURITY_CONFIG)
    patterns = load_patterns(_SECURITY_CONFIG)
    provider = _build_embedding_provider()

    eng = create_async_engine(database_url)
    try:
        async with eng.begin() as conn:
            await conn.execute(text(f"TRUNCATE {_TRUNCATE_TABLES} RESTART IDENTITY CASCADE"))
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as session:
            await run_source(
                session,
                source=LocalSource(
                    name="kiln-eval",
                    type="local",
                    path=str(_EVAL_CORPUS_DIR),
                    include=["*.md"],
                ),
                settings=settings,
                embedding_provider=provider,
                prompt_injection_phrases=phrases,
                sensitive_patterns=patterns,
            )
            await session.commit()
    finally:
        await eng.dispose()

    db = Database(database_url, pool_size=settings.pg_pool_size)
    retriever = HybridRetriever(
        db=db,
        embedding_provider=provider,
        config=load_retrieval_config(settings.security_config_path),
        ef_search=settings.hnsw_ef_search,
    )
    cases_out: list[dict[str, Any]] = []
    try:
        for case in cases:
            pack = await retriever.context_pack(
                case.query,
                filters=RetrievalFilters(**case.filters),
                task="review_precision_regrade",
                max_chunks=8,
                max_tokens=3000,
            )
            chunks = [
                {"citation": _citation_key(ev), "excerpt": ev.text[:600]}
                for ev in pack.evidence[:3]
            ]
            cases_out.append({"case_id": case.case_id, "query": case.query, "chunks": chunks})
    finally:
        await provider.aclose()
        await db.dispose()
    return {"rubric": {str(k): v for k, v in RUBRIC.items()}, "cases": cases_out}


# ─── CLI ───────────────────────────────────────────────────────────────


def _cmd_worksheet() -> int:
    database_url = os.environ.get("KILN_DATABASE_URL")
    if not database_url:
        print("KILN_DATABASE_URL is required for `worksheet`.", file=sys.stderr)
        return 2
    worksheet = asyncio.run(_build_worksheet(database_url))
    _WORKSHEET_OUT.parent.mkdir(parents=True, exist_ok=True)
    _WORKSHEET_OUT.write_text(yaml.safe_dump(worksheet, sort_keys=False), encoding="utf-8")
    n_pairs = sum(len(c["chunks"]) for c in worksheet["cases"])
    print(f"wrote {_WORKSHEET_OUT} — {len(worksheet['cases'])} cases, {n_pairs} pairs to grade")
    return 0


def _cmd_aggregate(judge_files: list[str]) -> int:
    if len(judge_files) < 2:
        print("aggregate needs >= 2 judge files (independent judges).", file=sys.stderr)
        return 2
    judge_grades: list[Mapping[str, Mapping[str, int]]] = []
    for path in judge_files:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        judge_grades.append(raw)
    pairs = aggregate_grades(judge_grades)
    print(f"# {len(judge_files)} judges — {_consensus_summary(pairs)}\n")
    print(_render_relevance_blocks(pairs))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("worksheet", help="emit the grading worksheet from the live retriever")
    agg = sub.add_parser("aggregate", help="median + consensus from per-judge grade files")
    agg.add_argument("judge_files", nargs="+", help="per-judge YAML grade files")
    args = parser.parse_args(argv)
    if args.command == "worksheet":
        return _cmd_worksheet()
    return _cmd_aggregate(args.judge_files)


if __name__ == "__main__":
    raise SystemExit(main())
