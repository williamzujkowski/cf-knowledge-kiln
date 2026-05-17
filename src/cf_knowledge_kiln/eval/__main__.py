"""CLI driver for the eval harness.

Usage::

    python -m cf_knowledge_kiln.eval \\
        --golden tests/eval/golden/docs.yaml \\
        --reports tests/eval/reports

The DB connection is resolved the usual way (``KILN_DATABASE_URL`` or
``VCAP_SERVICES``). The corpus must already be ingested — the CLI is
deliberately *not* an ingester. Use ``make ingest`` first, or, in
tests, the seeded conftest fixture.

Exit codes:

* ``0`` — all per-case ``must_appear_within_k`` constraints satisfied.
* ``1`` — at least one case missed (the markdown report shows which).
* ``2`` — usage error / no golden set / no DB binding.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db import Database, resolve_database_url
from cf_knowledge_kiln.eval import (
    EvalReport,
    GoldenCase,
    load_golden_set,
    run_eval,
    to_markdown,
    write_json,
    write_markdown,
)
from cf_knowledge_kiln.ingestion.embedding.factory import build_provider_from_settings
from cf_knowledge_kiln.retrieval import HybridRetriever, load_retrieval_config


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a Unix-style exit code."""
    args = _parse_args(argv)
    cases = load_golden_set(args.golden)

    settings = get_settings()
    url = resolve_database_url(settings)
    if url is None:
        print("error: no DB binding (set KILN_DATABASE_URL).", file=sys.stderr)
        return 2

    return asyncio.run(_run(url, cases, settings, args))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m cf_knowledge_kiln.eval")
    p.add_argument("--golden", type=Path, required=True, help="Path to golden YAML.")
    p.add_argument(
        "--reports",
        type=Path,
        default=Path("tests/eval/reports"),
        help="Directory for JSON + Markdown reports (default: tests/eval/reports).",
    )
    p.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=[1, 3, 5, 10],
        help="K values to score at (default: 1 3 5 10).",
    )
    return p.parse_args(argv)


async def _run(
    url: str,
    cases: list[GoldenCase],
    settings: Settings,
    args: argparse.Namespace,
) -> int:
    db = Database(url, pool_size=settings.pg_pool_size)
    try:
        provider = build_provider_from_settings(settings)
        config = load_retrieval_config(settings.security_config_path)
        retriever = HybridRetriever(
            db=db,
            embedding_provider=provider,
            config=config,
            ef_search=settings.hnsw_ef_search,
        )
        report = await run_eval(retriever, cases, k_values=tuple(args.k))
    finally:
        await db.dispose()

    args.reports.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    write_json(report, args.reports / f"{ts}.json")
    write_markdown(report, args.reports / f"{ts}.md")
    # "latest" is the operator's entry point; CI also reads from here.
    write_json(report, args.reports / "latest.json")
    write_markdown(report, args.reports / "latest.md")
    sys.stdout.write(to_markdown(report))

    return 0 if _all_within_bounds(report, cases) else 1


def _all_within_bounds(report: EvalReport, cases: list[GoldenCase]) -> bool:
    """Every expected hit must appear within its ``must_appear_within_k``."""
    case_index = {c.case_id: c for c in cases}
    for case_result in report.per_case:
        case = case_index.get(case_result.case_id)
        if case is None:  # pragma: no cover — defensive
            return False
        for hit, rank in zip(case.expected, case_result.per_hit_ranks, strict=True):
            if rank is None or rank >= hit.must_appear_within_k:
                return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
