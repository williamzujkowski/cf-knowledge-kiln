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

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.models import Document, DocumentChunk, IngestionRun
from cf_knowledge_kiln.db.repositories import (
    DataSourcesRepository,
    IngestionRunsRepository,
)
from cf_knowledge_kiln.ingestion._jsonsafe import jsonify
from cf_knowledge_kiln.ingestion.chunking import (
    FrontmatterTooLargeError,
    parse_document,
)
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
    """Per-source ingestion result. Mirrors `ingestion_runs.stats`.

    ``run_id`` is set after :func:`run_source` writes the initial
    ``ingestion_runs`` row, so a caller (e.g. the worker) can link
    a downstream ``ingestion_jobs.mark_done`` to the exact run that
    produced the work — important for the crash-recovery sweep
    described in issue #47.
    """

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
    run_id: UUID | None = None

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


def _resolve_doc_defaults(metadata: dict[str, Any], source_defaults: Source) -> dict[str, Any]:
    """Frontmatter wins; source defaults backfill missing keys.

    Pulled out of :func:`_upsert_document` so the SQL upsert stays
    readable top-to-bottom (#53 cleanup).
    """
    return {
        "status": metadata.get("status") or source_defaults.status,
        "owner": metadata.get("owner") or source_defaults.default_owner,
        "authority": metadata.get("authority") or source_defaults.authority,
        "sensitivity": metadata.get("sensitivity") or source_defaults.default_sensitivity,
        # #24: source_url drives the clickable source link on result
        # cards. Frontmatter-only for now (no source-level template);
        # operators add `source_url: https://...` to a doc to make its
        # card open the canonical URL in a new tab. Untrusted-input
        # policy: allowlist http(s) only — Pydantic's AnyUrl accepts
        # javascript:/data:/file: which would be a stored-XSS sink
        # when the template renders the value into an href.
        "source_url": _safe_source_url(metadata.get("source_url")),
    }


def _safe_source_url(raw: Any) -> str | None:
    """Return ``raw`` only when it parses as an http(s) URL; else None.

    Frontmatter is untrusted (per AGENTS.md). A malicious source could
    ship ``source_url: javascript:alert(document.cookie)`` and turn
    the result-card link into a clickable XSS payload. Reject anything
    that isn't an http(s) absolute URL at the ingestion boundary so
    the bad value never reaches the documents.source_url column.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    # urllib.parse handles the scheme detection without pulling in
    # validators or pydantic at the ingestion layer.
    from urllib.parse import urlparse

    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return cleaned


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
    SELECT-then-INSERT pattern into an ``IntegrityError``.
    """
    table = Document.__table__
    defaults = _resolve_doc_defaults(metadata, source_defaults)
    # Defensive coercion (#91): the parser already runs jsonify(), but
    # any future caller that hands us a custom metadata dict could
    # smuggle a non-JSON-native value past it. Idempotent on already-
    # safe inputs and cheap.
    safe_metadata = jsonify(metadata)
    insert_stmt = pg_insert(table).values(  # type: ignore[arg-type]
        {
            table.c.repo: repo,
            table.c.path: path,
            table.c.title: title,
            table.c.metadata: safe_metadata,
            table.c.commit_sha: commit_sha,
            **{table.c[k]: v for k, v in defaults.items()},
        }
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_documents_repo_path",
        set_={
            "title": insert_stmt.excluded.title,
            "metadata": insert_stmt.excluded.metadata,
            "commit_sha": insert_stmt.excluded.commit_sha,
            **{k: insert_stmt.excluded[k] for k in defaults},
        },
    ).returning(table.c.id)
    doc_id = (await session.execute(upsert_stmt)).scalar_one()
    return (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()


def _chunk_security_metadata(
    content: str,
    summary: IngestionSummary,
    *,
    prompt_injection_phrases: list[str] | None,
) -> dict[str, Any]:
    """Stamp ingest-time security markers on chunk metadata.

    Pulled out of :func:`_process_file` (#53 cleanup). The retrieval
    path emits ``prompt_injection_pattern`` warnings in O(1) per chunk
    using these markers — see #57. Empty phrase list → empty dict.
    """
    if not prompt_injection_phrases:
        return {}
    match = scan_prompt_injection(content, prompt_injection_phrases)
    if match is None:
        return {}
    summary.chunks_with_prompt_injection += 1
    return {
        "has_prompt_injection": True,
        "matched_pattern": match["matched_pattern"],
    }


async def _upsert_chunk(
    session: AsyncSession,
    *,
    doc_id: UUID,
    chunk: Any,  # parsed chunk; structural, not nominal
    summary: IngestionSummary,
    prompt_injection_phrases: list[str] | None,
) -> None:
    """Upsert one chunk row. Used by :func:`_process_file` (#53 cleanup).

    Upsert on ``(document_id, chunk_index)``: a content edit replaces
    the row in place so the chunk's UUID is stable. That stable ID is
    what ``chunk_embeddings`` references; the embedding pass will
    notice the ``content_hash`` drift and re-embed.
    """
    table = DocumentChunk.__table__
    chunk_extra = _chunk_security_metadata(
        chunk.content, summary, prompt_injection_phrases=prompt_injection_phrases
    )
    insert_stmt = pg_insert(table).values(  # type: ignore[arg-type]
        {
            table.c.document_id: doc_id,
            table.c.chunk_index: chunk.chunk_index,
            table.c.content: chunk.content,
            table.c.content_tokens: chunk.content_tokens,
            table.c.content_hash: chunk.content_hash,
            table.c.heading_path: chunk.heading_path,
            table.c.metadata: chunk_extra,
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
    try:
        parsed = parse_document(text)
    except FrontmatterTooLargeError as exc:
        # #54: oversize frontmatter would otherwise crash the file or
        # land a multi-megabyte JSONB blob. Record as a skip, keep
        # ingesting the rest of the corpus.
        summary.errors.append(f"{file.path}: frontmatter too large: {exc}")
        summary.files_skipped += 1
        _bump(summary.skip_reasons, "frontmatter_too_large")
        return
    if not parsed.chunks:
        summary.warnings.append(f"{file.path}: no chunks (empty or parse error)")
        summary.files_skipped += 1
        return
    doc = await _upsert_document(
        session,
        repo=_repo_label(source),
        path=file.path,
        title=parsed.title or file.path,
        metadata=parsed.meta,
        commit_sha=file.commit_sha,
        source_defaults=source,
    )
    touched_doc_ids.add(doc.id)
    existing_by_index = await _existing_chunks_by_index(session, doc.id)
    seen_indices: set[int] = set()
    for chunk in parsed.chunks:
        seen_indices.add(chunk.chunk_index)
        prev = existing_by_index.get(chunk.chunk_index)
        if prev is not None and prev[1] == chunk.content_hash:
            summary.chunks_unchanged += 1
            continue
        await _upsert_chunk(
            session,
            doc_id=doc.id,
            chunk=chunk,
            summary=summary,
            prompt_injection_phrases=prompt_injection_phrases,
        )
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
    summary.run_id = run.id
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
