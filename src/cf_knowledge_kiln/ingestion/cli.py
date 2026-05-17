"""Ingestion CLI.

Subcommands:

* ``validate`` — load the source allowlist and exit non-zero if the
  YAML / schema is invalid. Default subcommand.
* ``ingest`` — enqueue one ``full_resync`` job per active allowlisted
  source. Requires a reachable database. Returns immediately; the
  worker drains the queue.
* ``serve-worker`` — run the polling worker until SIGTERM/SIGINT.

Entrypoint: ``python -m cf_knowledge_kiln.ingestion <subcommand>``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from cf_knowledge_kiln.config import get_settings
from cf_knowledge_kiln.db import Database, resolve_database_url
from cf_knowledge_kiln.db.repositories import IngestionJobsRepository
from cf_knowledge_kiln.ingestion.sources import SourceAllowlist, SourceAllowlistError
from cf_knowledge_kiln.ingestion.worker import serve as _serve_worker

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path("config/sources.yaml")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cf_knowledge_kiln.ingestion",
        description="Ingest documentation from allowlisted sources.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"Path to sources.yaml (default: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="validate",
        choices=["validate", "ingest", "serve-worker"],
        help="Subcommand (default: validate).",
    )
    return parser


def _validate(allowlist_path: Path) -> int:
    try:
        allowlist = SourceAllowlist.from_yaml(allowlist_path)
    except SourceAllowlistError as exc:
        logger.error("Source allowlist failed validation: %s", exc)
        return 2
    active = sum(1 for _ in allowlist.active())
    logger.info(
        "Source allowlist OK: %d sources loaded (%d active) from %s",
        len(allowlist),
        active,
        allowlist_path,
    )
    return 0


async def _enqueue(allowlist_path: Path) -> int:
    settings = get_settings()
    try:
        allowlist = SourceAllowlist.from_yaml(allowlist_path)
    except SourceAllowlistError as exc:
        logger.error("Source allowlist failed validation: %s", exc)
        return 2
    url = resolve_database_url(settings)
    if url is None:
        logger.error(
            "cannot enqueue: no database URL (set KILN_DATABASE_URL or bind a Postgres service)"
        )
        return 2
    db = Database(url, pool_size=settings.pg_pool_size, max_overflow=settings.pg_pool_max_overflow)
    try:
        enqueued = 0
        for source in allowlist.active():
            async with db.session() as session:
                await IngestionJobsRepository(session).create(
                    kind="full_resync",
                    payload={"source_name": source.name},
                )
                await session.commit()
            enqueued += 1
            logger.info("enqueued %s", source.name)
        logger.info("enqueued %d job(s); start the worker to drain the queue", enqueued)
    finally:
        await db.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.config)
    if args.command == "ingest":
        return asyncio.run(_enqueue(args.config))
    if args.command == "serve-worker":
        return asyncio.run(_serve_worker(allowlist_path=args.config))
    logger.error("unknown command: %s", args.command)  # pragma: no cover
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
