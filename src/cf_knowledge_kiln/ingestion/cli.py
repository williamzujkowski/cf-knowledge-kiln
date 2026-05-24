"""Ingestion CLI.

Subcommands:

* ``validate`` — load the source allowlist and exit non-zero if the
  YAML / schema is invalid. Default subcommand.
* ``ingest`` — enqueue one ``full_resync`` job per active allowlisted
  source. Requires a reachable database. Returns immediately; the
  worker drains the queue.
* ``serve-worker`` — run the polling worker until SIGTERM/SIGINT.
* ``reembed`` — re-embed every existing chunk through the active
  provider (#224). Use after a model swap or a prefix-handling fix
  like #216. ``--dry-run`` previews the chunk count without writing.

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
from cf_knowledge_kiln.ingestion.embedding.factory import build_provider_from_settings
from cf_knowledge_kiln.ingestion.reembed import reembed_all_chunks
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
        "--dry-run",
        action="store_true",
        help=(
            "For ``reembed``: print the chunk count + active provider "
            "without writing anything. No effect on other subcommands."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="validate",
        choices=["validate", "ingest", "serve-worker", "reembed"],
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


async def _reembed(*, dry_run: bool) -> int:
    """#224: re-embed every chunk through the active provider.

    Bypasses the allowlist — operates on whatever chunks are already
    in the DB. So a stuck source.yaml doesn't block the re-embed
    after a model swap or prefix-handling fix.
    """
    settings = get_settings()
    url = resolve_database_url(settings)
    if url is None:
        logger.error(
            "cannot reembed: no database URL (set KILN_DATABASE_URL or bind a Postgres service)"
        )
        return 2
    provider = build_provider_from_settings(settings)
    if provider is None:
        logger.error(
            "cannot reembed: no embedding provider configured "
            "(check config/models.yaml::models.embedding)"
        )
        return 2
    db = Database(url, pool_size=settings.pg_pool_size, max_overflow=settings.pg_pool_max_overflow)
    try:
        async with db.session() as session, session.begin():
            result = await reembed_all_chunks(
                session,
                provider=provider,
                batch_size=settings.ingest_embed_batch_size,
                concurrency=settings.ingest_embed_concurrency,
                dry_run=dry_run,
            )
        if dry_run:
            logger.info(
                "reembed dry-run: would re-embed %d chunks via %s/%s",
                result.chunks_total,
                provider.provider,
                provider.model,
            )
        else:
            logger.info(
                "reembed done: %d embedded, %d failed, %d total via %s/%s",
                result.chunks_embedded,
                result.chunks_failed,
                result.chunks_total,
                provider.provider,
                provider.model,
            )
        # Non-zero exit only on TOTAL failure — any partial success
        # is still a useful re-embed result that we don't want to
        # signal as broken to a scripted caller.
        if not dry_run and result.chunks_total > 0 and result.chunks_embedded == 0:
            return 1
        return 0
    finally:
        await provider.aclose()
        await db.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.config)
    if args.command == "ingest":
        return asyncio.run(_enqueue(args.config))
    if args.command == "serve-worker":
        return asyncio.run(_serve_worker(allowlist_path=args.config))
    if args.command == "reembed":
        return asyncio.run(_reembed(dry_run=args.dry_run))
    logger.error("unknown command: %s", args.command)  # pragma: no cover
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
