"""Ingestion worker entrypoint (#40).

The worker:

1. Loads :class:`SourceAllowlist` at startup; refuses to start if the
   YAML is malformed (the CLI's ``serve-worker`` subcommand catches the
   exit code).
2. Polls the ``ingestion_jobs`` queue every
   ``KILN_INGEST_POLL_INTERVAL_SECONDS`` seconds.
3. Claims one job at a time via ``IngestionJobsRepository.claim_one``
   (which uses ``FOR UPDATE SKIP LOCKED`` — safe under multiple
   workers).
4. Resolves the job's source from the allowlist; runs
   :func:`pipeline.run_source`; marks the job done with the resulting
   ``ingestion_runs`` row id.
5. Exits cleanly on ``SIGTERM`` / ``SIGINT`` — never marks a half-done
   job as succeeded.

This module is invoked by ``scripts/start-worker.sh`` and by
``python -m cf_knowledge_kiln.ingestion serve-worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Any

from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db import Database, resolve_database_url
from cf_knowledge_kiln.db.repositories import IngestionJobsRepository
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import (
    SourceAllowlist,
    SourceAllowlistError,
    SourceNotAllowedError,
)

logger = logging.getLogger(__name__)


class Worker:
    """Polling worker.

    Constructed with a ``Database``, a ``SourceAllowlist``, and a
    ``Settings`` object. The poll cadence and source name resolution
    are driven by the queue's job payloads (``payload['source_name']``).
    """

    def __init__(
        self,
        *,
        db: Database,
        allowlist: SourceAllowlist,
        settings: Settings,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self._db = db
        self._allowlist = allowlist
        self._settings = settings
        self._poll = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else settings.ingest_poll_interval_seconds
        )
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        """Idempotent. The next poll-cycle wake exits cleanly."""
        self._shutdown.set()

    async def run_forever(self) -> None:
        logger.info("worker started; poll interval %.1fs", self._poll)
        while not self._shutdown.is_set():
            processed = await self._tick()
            if processed:
                continue  # immediately check for more work
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._shutdown.wait(), timeout=self._poll)
        logger.info("worker shutdown complete")

    async def _tick(self) -> bool:
        """One poll cycle. Returns True iff a job was processed."""
        async with self._db.session() as session:
            jobs = IngestionJobsRepository(session)
            job = await jobs.claim_one()
            if job is None:
                await session.commit()
                return False
            await session.commit()
        try:
            await self._process(job.id, job.payload)
        except Exception as exc:
            logger.exception("job %s failed", job.id)
            async with self._db.session() as session:
                await IngestionJobsRepository(session).mark_failed(job.id, error=str(exc))
                await session.commit()
        return True

    async def _process(self, job_id: Any, payload: dict[str, Any]) -> None:
        source_name = payload.get("source_name")
        if not isinstance(source_name, str):
            raise ValueError(f"job {job_id} payload missing 'source_name' string; got {payload!r}")
        source = self._allowlist.get(source_name)  # raises SourceNotAllowedError
        async with self._db.session() as session:
            summary = await run_source(session, source=source, settings=self._settings)
            await session.commit()
        async with self._db.session() as session:
            await IngestionJobsRepository(session).mark_done(job_id)
            await session.commit()
        logger.info(
            "job %s done: %d indexed, %d skipped, %d chunks created",
            job_id,
            summary.files_indexed,
            summary.files_skipped,
            summary.chunks_created,
        )


def _install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.request_shutdown)


async def serve(
    *,
    allowlist_path: Path,
    settings: Settings | None = None,
) -> int:
    """Top-level coroutine for ``serve-worker``. Returns process exit code."""
    settings = settings or get_settings()
    try:
        allowlist = SourceAllowlist.from_yaml(allowlist_path)
    except SourceAllowlistError as exc:
        logger.error("worker refused to start: %s", exc)
        return 2

    url = resolve_database_url(settings)
    if url is None:
        logger.error(
            "worker refused to start: no database URL "
            "(set KILN_DATABASE_URL or bind a Postgres service)"
        )
        return 2

    db = Database(url, pool_size=settings.pg_pool_size, max_overflow=settings.pg_pool_max_overflow)
    worker = Worker(db=db, allowlist=allowlist, settings=settings)
    _install_signal_handlers(worker)
    try:
        await worker.run_forever()
    finally:
        await db.dispose()
    return 0


__all__ = ["SourceNotAllowedError", "Worker", "serve"]
