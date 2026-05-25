"""End-to-end ingestion pipeline.

One call to :func:`run_source` takes a :class:`Source` (allowlisted),
fetches its files (git or local), parses each into chunks, upserts the
documents + chunks into Postgres, embeds anything new-or-changed, and
writes a summary row into ``ingestion_runs``.

Two idempotency properties matter here:

* **Chunks** — re-running against unchanged content writes zero new
  chunk rows. The hash on each chunk is the gate.
* **Embeddings** (Phase 4) — re-running against unchanged content
  makes zero embedding-provider calls. The gate is the per-chunk
  ``content_hash`` stored on ``chunk_embeddings``: if it equals the
  current chunk hash, the embedding is up to date.

The embedding pass runs after the chunk pass so it can rely on
flushed chunk IDs.

This module owns the orchestration; the per-file parse → upsert path
lives in :mod:`cf_knowledge_kiln.ingestion._file_processing` and the
:class:`IngestionSummary` result type in
:mod:`cf_knowledge_kiln.ingestion._summary` — split out so each file
stays under the 400-line cap (#169).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.models import IngestionRun
from cf_knowledge_kiln.db.repositories import (
    DataSourcesRepository,
    IngestionRunsRepository,
)
from cf_knowledge_kiln.ingestion._file_processing import _process_file, _repo_label
from cf_knowledge_kiln.ingestion._summary import IngestionSummary, _bump
from cf_knowledge_kiln.ingestion.connectors import (
    IngestionCapExceeded,
    IngestionCaps,
    fetch_source,
)
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.pipeline import embed_touched_documents
from cf_knowledge_kiln.ingestion.git_credentials import GitCredentials
from cf_knowledge_kiln.ingestion.sources import Source


def _caps_from_settings(settings: Settings) -> IngestionCaps:
    return IngestionCaps(
        max_file_bytes=settings.ingest_max_file_bytes,
        max_files=settings.ingest_max_files,
        max_repo_bytes=settings.ingest_max_repo_bytes,
    )


async def run_source(
    session: AsyncSession,
    *,
    source: Source,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None = None,
    prompt_injection_phrases: list[str] | None = None,
    sensitive_patterns: list[Any] | None = None,
    git_credentials: GitCredentials | None = None,
) -> IngestionSummary:
    """Run the full pipeline for a single source. Writes an ingestion_runs row.

    Commits internally on success and on cap-violation so the
    ``ingestion_runs`` row is durable even if the caller never commits
    (e.g. caller crashes after this returns). Callers that want to
    compose this in a larger transaction should pass a session bound
    to that transaction; the internal commits then commit only the
    work this function did.

    ``embedding_provider`` is optional so the pipeline still runs
    chunk-only when no provider is configured (e.g. a worker started
    before Phase 4 config landed). When supplied, embeddings are
    generated for any chunk whose stored ``content_hash`` doesn't
    match the current chunk hash — re-ingestion of unchanged content
    therefore makes zero provider calls (issue #18).
    """
    summary = IngestionSummary()
    runs_repo = IngestionRunsRepository(session)
    sources_repo = DataSourcesRepository(session)

    src_row = await _ensure_data_source_row(sources_repo, source)
    run = await runs_repo.create(source_id=src_row.id, status="running")
    summary.run_id = run.id
    # #239: commit the catalog setup BEFORE the long embed phase.
    # The data_sources INSERT (or UPSERT, since _ensure_data_source_row
    # now uses ON CONFLICT) holds a row-level lock on the unique-by-
    # `name` constraint until commit. The embed phase runs for minutes
    # on CPU, so without this commit a concurrent worker processing
    # any job for the same source name blocks indefinitely waiting for
    # the lock — observed in homelab CF deploy as "ingestion_jobs
    # attempts climbs to 10 with zero rows landing in documents". Same
    # logic for `ingestion_runs` (running status is what we want
    # durable for the recovery sweep anyway). Both rows are reference/
    # audit data, not work-in-progress that should rollback together
    # with the embed phase.
    await session.commit()
    try:
        fetch = fetch_source(source, _caps_from_settings(settings), credentials=git_credentials)
    except IngestionCapExceeded as exc:
        summary.errors.append(str(exc))
        await session.execute(
            _runs_update(run.id, status="failed", stats=summary.as_stats(), errors=summary.errors)
        )
        await session.commit()
        return summary

    summary.files_scanned = len(fetch.files) + len(fetch.skipped)
    for skipped in fetch.skipped:
        summary.files_skipped += 1
        _bump(summary.skip_reasons, skipped.reason)
    touched_doc_ids: set[UUID] = set()
    for file in fetch.files:
        await _process_file(
            session,
            file=file,
            source=source,
            summary=summary,
            touched_doc_ids=touched_doc_ids,
            prompt_injection_phrases=prompt_injection_phrases,
            sensitive_patterns=sensitive_patterns,
        )

    if embedding_provider is not None:
        try:
            await embed_touched_documents(
                session,
                doc_ids=touched_doc_ids,
                provider=embedding_provider,
                summary=summary,
                batch_size=settings.ingest_embed_batch_size,
                concurrency=settings.ingest_embed_concurrency,
            )
        except Exception as exc:
            # #151: the embed pass raises only when EVERY batch failed,
            # which means no partial-success work is at risk. Record it
            # as a run-level failure so the ingestion_runs row reflects
            # the hopeless case while the per-batch breadcrumbs already
            # added to summary.errors stay intact for forensics.
            summary.errors.append(f"embedding pass failed (all batches): {exc}")

    await session.execute(
        _runs_update(
            run.id,
            status="succeeded" if not summary.errors else "partial",
            stats=summary.as_stats(),
            warnings=summary.warnings,
            errors=summary.errors,
            commit_sha=fetch.commit_sha,
        )
    )
    await session.commit()
    return summary


async def _ensure_data_source_row(repo: DataSourcesRepository, source: Source) -> Any:
    """Idempotent upsert of the row for ``source.name``; returns the live row.

    Uses :meth:`DataSourcesRepository.get_or_create` rather than the
    historical ``list()`` → ``create()`` pattern, which had two bugs:

    1. **Race window** between the list and the create: two workers
       starting the same source ingest concurrently could both find no
       existing row, then both INSERT, with the loser hitting the
       UNIQUE-on-name constraint and crashing.
    2. **Long-held lock**: the INSERT held a row-level lock on the
       new row until the surrounding transaction committed, which
       only happened minutes later after the embed phase. A second
       worker upserting the SAME name blocked for the entire embed
       duration. Combined with the structural deadlock from #239,
       this manifested as worker spin loops with zero progress on
       multi-instance CF deploys.

    The UPSERT keeps both behaviors atomic + lock-short.
    """
    return await repo.get_or_create(
        name=source.name,
        type=source.__class__.__name__.replace("Source", "").lower(),
        location=_repo_label(source),
    )


def _runs_update(
    run_id: Any,
    *,
    status: str,
    stats: dict[str, Any],
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    commit_sha: str | None = None,
) -> Any:
    # #202: ``clock_timestamp()`` not ``now()``. Postgres' ``now()`` (and
    # the equivalent ``transaction_timestamp()``) returns the
    # START-OF-TRANSACTION time. ``run_source`` opens one transaction
    # for the whole ingestion run — the ``runs_repo.create`` row
    # (started_at default = ``now()``) and this terminal UPDATE
    # (finished_at) commit together, so both timestamps would be equal
    # to the microsecond and any wall-clock duration would be zero.
    # ``clock_timestamp()`` reads the actual wall clock at the moment
    # the UPDATE executes, so ``finished_at - started_at`` recovers
    # the real run duration.
    return (
        update(IngestionRun)
        .where(IngestionRun.id == run_id)
        .values(
            status=status,
            finished_at=func.clock_timestamp(),
            stats=stats,
            warnings=warnings or [],
            errors=errors or [],
            commit_sha=commit_sha,
        )
    )


__all__ = [
    "IngestionSummary",
    "run_source",
]
