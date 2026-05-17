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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.models import Document, DocumentChunk
from cf_knowledge_kiln.db.repositories import (
    DataSourcesRepository,
    IngestionRunsRepository,
)
from cf_knowledge_kiln.ingestion.chunking import parse_document
from cf_knowledge_kiln.ingestion.connectors import (
    FetchedFile,
    IngestionCapExceeded,
    IngestionCaps,
    fetch_source,
)
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.pipeline import embed_touched_documents
from cf_knowledge_kiln.ingestion.prompt_injection import scan as scan_prompt_injection
from cf_knowledge_kiln.ingestion.sources import Source

logger = logging.getLogger(__name__)


@dataclass
class IngestionSummary:
    """Per-source ingestion result. Mirrors `ingestion_runs.stats`."""

    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    chunks_created: int = 0
    chunks_unchanged: int = 0
    chunks_with_prompt_injection: int = 0
    embeddings_created: int = 0
    embeddings_unchanged: int = 0
    embeddings_failed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def as_stats(self) -> dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "chunks_created": self.chunks_created,
            "chunks_unchanged": self.chunks_unchanged,
            "chunks_with_prompt_injection": self.chunks_with_prompt_injection,
            "embeddings_created": self.embeddings_created,
            "embeddings_unchanged": self.embeddings_unchanged,
            "embeddings_failed": self.embeddings_failed,
            "skip_reasons": self.skip_reasons,
        }


def _bump(d: dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1


async def _existing_chunks_by_index(
    session: AsyncSession, document_id: Any
) -> dict[int, tuple[UUID, str]]:
    """Return ``{chunk_index: (chunk_id, content_hash)}`` for one document.

    Used by ``_process_file`` to decide per-index whether to skip,
    upsert, or replace each chunk. The index is the natural key the
    parser produces; the DB enforces ``UNIQUE (document_id, chunk_index)``.
    """
    stmt = select(DocumentChunk.id, DocumentChunk.chunk_index, DocumentChunk.content_hash).where(
        DocumentChunk.document_id == document_id
    )
    return {idx: (chunk_id, h) for chunk_id, idx, h in (await session.execute(stmt)).all()}


async def _upsert_document(
    session: AsyncSession,
    *,
    repo: str,
    path: str,
    title: str,
    metadata: dict[str, Any],
    commit_sha: str | None,
    source_defaults: Source,
) -> Document:
    """Upsert ``documents`` row keyed by ``(repo, path)``. Returns the live row.

    Uses ``INSERT ... ON CONFLICT (repo, path) DO UPDATE`` so two
    concurrent workers (or a retry after crash) can't race the
    SELECT-then-INSERT pattern into an ``IntegrityError``. We address
    columns via ``Document.__table__`` so the SQL column name
    ``metadata`` doesn't collide with SQLAlchemy's reserved
    ``Base.metadata`` attribute.
    """
    table = Document.__table__
    status = metadata.get("status") or source_defaults.status
    owner = metadata.get("owner") or source_defaults.default_owner
    authority = metadata.get("authority") or source_defaults.authority
    sensitivity = metadata.get("sensitivity") or source_defaults.default_sensitivity
    insert_stmt = pg_insert(table).values(  # type: ignore[arg-type]
        {
            table.c.repo: repo,
            table.c.path: path,
            table.c.title: title,
            table.c.metadata: metadata,
            table.c.commit_sha: commit_sha,
            table.c.status: status,
            table.c.owner: owner,
            table.c.authority: authority,
            table.c.sensitivity: sensitivity,
        }
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_documents_repo_path",
        set_={
            "title": insert_stmt.excluded.title,
            "metadata": insert_stmt.excluded.metadata,
            "commit_sha": insert_stmt.excluded.commit_sha,
            "status": insert_stmt.excluded.status,
            "owner": insert_stmt.excluded.owner,
            "authority": insert_stmt.excluded.authority,
            "sensitivity": insert_stmt.excluded.sensitivity,
        },
    ).returning(table.c.id)
    doc_id = (await session.execute(upsert_stmt)).scalar_one()
    return (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()


async def _process_file(
    session: AsyncSession,
    *,
    file: FetchedFile,
    source: Source,
    summary: IngestionSummary,
    touched_doc_ids: set[UUID],
    prompt_injection_phrases: list[str] | None = None,
) -> None:
    """Parse one fetched file and write its chunks. Dedup on content_hash."""
    try:
        text = file.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        summary.errors.append(f"{file.path}: not utf-8: {exc}")
        summary.files_skipped += 1
        _bump(summary.skip_reasons, "binary_content")
        return
    parsed = parse_document(text)
    if not parsed.chunks:
        summary.warnings.append(f"{file.path}: no chunks (empty or parse error)")
        summary.files_skipped += 1
        return
    title = parsed.title or file.path
    doc = await _upsert_document(
        session,
        repo=_repo_label(source),
        path=file.path,
        title=title,
        metadata=parsed.meta,
        commit_sha=file.commit_sha,
        source_defaults=source,
    )
    touched_doc_ids.add(doc.id)
    existing_by_index = await _existing_chunks_by_index(session, doc.id)
    seen_indices: set[int] = set()
    chunks_table = DocumentChunk.__table__
    for chunk in parsed.chunks:
        seen_indices.add(chunk.chunk_index)
        prev = existing_by_index.get(chunk.chunk_index)
        if prev is not None and prev[1] == chunk.content_hash:
            summary.chunks_unchanged += 1
            continue
        # Stamp ingest-time security markers on chunk metadata so the
        # retrieval path can emit prompt_injection_pattern warnings in
        # O(1) per chunk (#57). The matcher returns the first match;
        # we record it as evidence for the warning. Empty phrase list
        # (no security config) → no metadata change.
        chunk_extra: dict[str, Any] = {}
        if prompt_injection_phrases:
            match = scan_prompt_injection(chunk.content, prompt_injection_phrases)
            if match is not None:
                chunk_extra["has_prompt_injection"] = True
                chunk_extra["matched_pattern"] = match["matched_pattern"]
                summary.chunks_with_prompt_injection += 1
        # Upsert on (document_id, chunk_index): a content edit replaces
        # the row in place so the chunk's UUID is stable. That stable ID
        # is what chunk_embeddings references; the embedding pass will
        # then notice the content_hash drift and re-embed.
        insert_stmt = pg_insert(chunks_table).values(  # type: ignore[arg-type]
            {
                chunks_table.c.document_id: doc.id,
                chunks_table.c.chunk_index: chunk.chunk_index,
                chunks_table.c.content: chunk.content,
                chunks_table.c.content_tokens: chunk.content_tokens,
                chunks_table.c.content_hash: chunk.content_hash,
                chunks_table.c.heading_path: chunk.heading_path,
                chunks_table.c.metadata: chunk_extra,
            }
        )
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_chunks_doc_index",
            set_={
                "content": insert_stmt.excluded.content,
                "content_tokens": insert_stmt.excluded.content_tokens,
                "content_hash": insert_stmt.excluded.content_hash,
                "heading_path": insert_stmt.excluded.heading_path,
                "metadata": insert_stmt.excluded.metadata,
            },
        )
        await session.execute(upsert_stmt)
        summary.chunks_created += 1
    orphan_ids = [
        chunk_id for idx, (chunk_id, _h) in existing_by_index.items() if idx not in seen_indices
    ]
    if orphan_ids:
        await session.execute(delete(DocumentChunk).where(DocumentChunk.id.in_(orphan_ids)))
    summary.files_indexed += 1


def _repo_label(source: Source) -> str:
    """Return a stable repo label for ``documents.repo``."""
    repo = getattr(source, "repo", None)
    if isinstance(repo, str) and repo:
        return repo
    return source.name


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
    try:
        fetch = fetch_source(source, _caps_from_settings(settings))
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
        )

    if embedding_provider is not None:
        await embed_touched_documents(
            session,
            doc_ids=touched_doc_ids,
            provider=embedding_provider,
            summary=summary,
        )

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
    """Insert the data_sources row if missing; return the live row."""
    existing = await repo.list()
    for row in existing:
        if row.name == source.name:
            return row
    return await repo.create(
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
    from sqlalchemy import func, update

    from cf_knowledge_kiln.db.models import IngestionRun

    return (
        update(IngestionRun)
        .where(IngestionRun.id == run_id)
        .values(
            status=status,
            finished_at=func.now(),
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
