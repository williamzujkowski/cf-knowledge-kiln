"""End-to-end ingestion pipeline.

One call to :func:`run_source` takes a :class:`Source` (allowlisted),
fetches its files (git or local), parses each into chunks, upserts the
documents + chunks into Postgres, and writes a summary row into
``ingestion_runs``.

Re-running against unchanged content is a no-op for chunks: the
content-hash check skips any chunk whose hash already exists for the
same document. Embedding work is Phase 4; this pipeline writes
document/chunk metadata only, so "no embedding work" is satisfied
trivially today and the hash check is what makes the property hold
when embeddings land.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
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
    SkippedFile,
    fetch_source,
)
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
            "skip_reasons": self.skip_reasons,
        }


def _bump(d: dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1


async def _existing_chunk_hashes(session: AsyncSession, document_id: Any) -> set[str]:
    stmt = select(DocumentChunk.content_hash).where(DocumentChunk.document_id == document_id)
    return set((await session.execute(stmt)).scalars().all())


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
    """Upsert ``documents`` row keyed by (repo, path). Returns the live row.

    Plain ORM (select-then-mutate-or-add). ``Document.extra`` is the
    Python attribute that maps to the SQL column ``metadata``; we use
    the attribute name throughout to avoid colliding with SQLAlchemy's
    reserved ``Base.metadata``.
    """
    existing = (
        await session.execute(select(Document).where(Document.repo == repo, Document.path == path))
    ).scalar_one_or_none()
    status = str(metadata.get("status", source_defaults.status))
    owner = metadata.get("owner") or source_defaults.default_owner
    authority = str(metadata.get("authority", source_defaults.authority))
    sensitivity = str(metadata.get("sensitivity", source_defaults.default_sensitivity))
    if existing is None:
        doc = Document(
            repo=repo,
            path=path,
            title=title,
            extra=metadata,
            commit_sha=commit_sha,
            status=status,
            owner=owner,
            authority=authority,
            sensitivity=sensitivity,
        )
        session.add(doc)
        await session.flush()
        return doc
    existing.title = title
    existing.extra = metadata
    existing.commit_sha = commit_sha
    existing.status = status
    existing.owner = owner
    existing.authority = authority
    existing.sensitivity = sensitivity
    await session.flush()
    return existing


async def _process_file(
    session: AsyncSession,
    *,
    file: FetchedFile,
    source: Source,
    summary: IngestionSummary,
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
    existing_hashes = await _existing_chunk_hashes(session, doc.id)
    for chunk in parsed.chunks:
        if chunk.content_hash in existing_hashes:
            summary.chunks_unchanged += 1
            continue
        session.add(
            DocumentChunk(
                document_id=doc.id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                content_tokens=chunk.content_tokens,
                content_hash=chunk.content_hash,
                heading_path=chunk.heading_path,
            )
        )
        summary.chunks_created += 1
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
) -> IngestionSummary:
    """Run the full pipeline for a single source. Writes an ingestion_runs row."""
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
        return summary

    summary.files_scanned = len(fetch.files) + len(fetch.skipped)
    for skipped in fetch.skipped:
        summary.files_skipped += 1
        _bump(summary.skip_reasons, skipped.reason)
    for file in fetch.files:
        await _process_file(session, file=file, source=source, summary=summary)

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


# ─── helpers exposed for tests ──────────────────────────────────────


__all__ = [
    "IngestionSummary",
    "run_source",
]


def _record_skipped(summary: IngestionSummary, skipped: SkippedFile) -> None:  # pragma: no cover
    summary.files_skipped += 1
    _bump(summary.skip_reasons, skipped.reason)
