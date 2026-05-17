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
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.factory import build_provider_from_settings
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.prompt_injection import load_phrases
from cf_knowledge_kiln.ingestion.sensitive_content import load_patterns
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
        embedding_provider: EmbeddingProvider | None = None,
        prompt_injection_phrases: list[str] | None = None,
        sensitive_patterns: list[Any] | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self._db = db
        self._allowlist = allowlist
        self._settings = settings
        self._embedding_provider = embedding_provider
        self._prompt_injection_phrases = prompt_injection_phrases
        self._sensitive_patterns = sensitive_patterns
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
        # Recovery sweep: requeue jobs left in `running` by a previous
        # process that died mid-job (SIGKILL, OOM, container eviction).
        # Without this, those rows would sit forever because the lock
        # released when the dead process's session closed.
        await self._recover_stale_running()
        while not self._shutdown.is_set():
            processed = await self._tick()
            if processed:
                continue  # immediately check for more work
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._shutdown.wait(), timeout=self._poll)
        logger.info("worker shutdown complete")

    async def _recover_stale_running(self) -> None:
        """Reconcile any `running` rows left over from a crashed worker.

        Three possibilities for each row:

        1. ``result_run_id`` is set AND the referenced ``ingestion_runs``
           row is ``succeeded`` / ``partial`` → the work is durably
           persisted; the crash happened between the run-update commit
           and the job-update commit. Mark the job ``succeeded``
           instead of redoing work (issue #47).
        2. ``result_run_id`` is set but the run is still ``running`` or
           ``failed`` → the work didn't finish; requeue.
        3. ``result_run_id`` is unset → never got past run_source's
           opening transaction; requeue.

        A clean shutdown finishes its current job first; this sweep
        only ever sees rows from hard kills (SIGKILL, OOM, container
        eviction).
        """
        from cf_knowledge_kiln.db.models import IngestionRun

        recovered_marked_done = 0
        recovered_requeued = 0
        async with self._db.session() as session:
            jobs = IngestionJobsRepository(session)
            stale = await jobs.list(status="running")
            for job in stale:
                if job.result_run_id is not None:
                    run = await session.get(IngestionRun, job.result_run_id)
                    if run is not None and run.status in ("succeeded", "partial"):
                        await jobs.mark_done(job.id, result_run_id=job.result_run_id)
                        recovered_marked_done += 1
                        continue
                await jobs.requeue(job.id)
                recovered_requeued += 1
            await session.commit()
        if recovered_marked_done:
            logger.warning(
                "reconciled %d orphaned job(s) to succeeded "
                "(work was durably persisted before crash)",
                recovered_marked_done,
            )
        if recovered_requeued:
            logger.warning("requeued %d orphaned running job(s) at startup", recovered_requeued)

    async def _tick(self) -> bool:
        """One poll cycle. Returns True iff a job was processed.

        Catches ``BaseException`` around ``_process`` so a
        ``CancelledError`` from a SIGTERM mid-job still marks the job
        failed instead of leaving it as a permanently-``running``
        zombie. The exception is re-raised so the outer loop honors
        the cancellation.
        """
        async with self._db.session() as session:
            jobs = IngestionJobsRepository(session)
            job = await jobs.claim_one()
            if job is None:
                await session.commit()
                return False
            await session.commit()
        try:
            await self._process(job.id, job.payload)
        except asyncio.CancelledError:
            await self._mark_failed(job.id, "worker cancelled mid-job (SIGTERM)")
            raise
        except Exception as exc:
            logger.exception("job %s failed", job.id)
            await self._mark_failed(job.id, str(exc))
        return True

    async def _mark_failed(self, job_id: Any, error: str) -> None:
        async with self._db.session() as session:
            await IngestionJobsRepository(session).mark_failed(job_id, error=error)
            await session.commit()

    async def _process(self, job_id: Any, payload: dict[str, Any]) -> None:
        source_name = payload.get("source_name")
        if not isinstance(source_name, str):
            raise ValueError(f"job {job_id} payload missing 'source_name' string; got {payload!r}")
        source = self._allowlist.get(source_name)  # raises SourceNotAllowedError
        # Single session for the whole job (#47). run_source commits
        # internally on success / cap-violation; mark_done runs in a
        # follow-up transaction on the same session + connection. This
        # halves the per-job pool checkouts vs the old two-session
        # design and lets us link mark_done to the run_id for the
        # recovery sweep to recognize crashes that happened AFTER
        # work was durably persisted.
        async with self._db.session() as session:
            summary = await run_source(
                session,
                source=source,
                settings=self._settings,
                embedding_provider=self._embedding_provider,
                prompt_injection_phrases=self._prompt_injection_phrases,
                sensitive_patterns=self._sensitive_patterns,
            )
            await IngestionJobsRepository(session).mark_done(job_id, result_run_id=summary.run_id)
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
    provider = build_provider_from_settings(settings)
    phrases = load_phrases(settings.security_config_path)
    sensitive = load_patterns(settings.security_config_path)
    worker = Worker(
        db=db,
        allowlist=allowlist,
        settings=settings,
        embedding_provider=provider,
        prompt_injection_phrases=phrases,
        sensitive_patterns=sensitive,
    )
    _install_signal_handlers(worker)
    try:
        await worker.run_forever()
    finally:
        if provider is not None:
            await provider.aclose()
        await db.dispose()
    return 0


__all__ = ["SourceNotAllowedError", "Worker", "serve"]
