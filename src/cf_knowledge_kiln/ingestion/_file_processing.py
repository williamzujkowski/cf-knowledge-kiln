"""Per-file ingestion processing (#169).

Split out of :mod:`cf_knowledge_kiln.ingestion.pipeline` to keep both
files under the 400-line cap. This module owns the "one fetched file →
upserted document + chunks" path; ``pipeline.run_source`` owns the
orchestration around it (fetch, the embedding pass, the
``ingestion_runs`` row).

Import graph: ``_summary`` ← ``_file_processing`` ← ``pipeline`` — no
cycle. These functions are package-private; ``run_source`` is the
public entry point.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.db.models import Document, DocumentChunk
from cf_knowledge_kiln.ingestion._jsonsafe import jsonify
from cf_knowledge_kiln.ingestion._summary import IngestionSummary, _bump
from cf_knowledge_kiln.ingestion.chunking import (
    FrontmatterTooLargeError,
    parse_document,
)
from cf_knowledge_kiln.ingestion.connectors import FetchedFile
from cf_knowledge_kiln.ingestion.prompt_injection import scan as scan_prompt_injection
from cf_knowledge_kiln.ingestion.sensitive_content import scan as scan_sensitive_content
from cf_knowledge_kiln.ingestion.sources import Source


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
        # #100: last_reviewed feeds the freshness boost + the
        # stale_source warning. Frontmatter ships an ISO date which
        # yaml.safe_load already returns as datetime.date; the
        # parser-side jsonify() converts it to "YYYY-MM-DD" so we
        # coerce back to date here for the SQLA column.
        "last_reviewed": _coerce_iso_date(metadata.get("last_reviewed")),
    }


def _coerce_iso_date(raw: Any) -> Any:
    """Convert an ISO-8601 date string back to ``datetime.date``.

    The parser runs ``jsonify`` over the frontmatter dict before
    handing it to us, which turns ``date(2024, 1, 15)`` into
    ``"2024-01-15"``. The ``documents.last_reviewed`` column is
    ``DATE``, so the SQLA layer needs a real date object. None /
    bad values pass through as None so the upsert leaves the column
    NULL (the existing default).
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


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
    sensitive_patterns: list[Any] | None = None,
) -> dict[str, Any]:
    """Stamp ingest-time security markers on chunk metadata.

    Two scanners run side by side:

    * Prompt-injection — substring match against ``content_filters
      .prompt_injection_phrases`` (#57). Stamps ``has_prompt_injection``
      + ``matched_pattern``.
    * Sensitive-content — regex match against ``content_filters
      .sensitive_patterns`` (#100). Stamps ``has_sensitive_content``
      + ``matched_sensitive_pattern``. The agent serializer drops
      stamped chunks from the body entirely per AGENTS.md.

    Either match flips its own boolean; the two run independently so a
    chunk can carry both.
    """
    out: dict[str, Any] = {}
    if prompt_injection_phrases:
        pi_match = scan_prompt_injection(content, prompt_injection_phrases)
        if pi_match is not None:
            summary.chunks_with_prompt_injection += 1
            out["has_prompt_injection"] = True
            out["matched_pattern"] = pi_match["matched_pattern"]
    if sensitive_patterns:
        sc_match = scan_sensitive_content(content, sensitive_patterns)
        if sc_match is not None:
            summary.chunks_with_sensitive_content += 1
            out["has_sensitive_content"] = True
            out["matched_sensitive_pattern"] = sc_match["matched_pattern"]
    return out


async def _upsert_chunk(
    session: AsyncSession,
    *,
    doc_id: UUID,
    chunk: Any,  # parsed chunk; structural, not nominal
    summary: IngestionSummary,
    prompt_injection_phrases: list[str] | None,
    sensitive_patterns: list[Any] | None = None,
) -> None:
    """Upsert one chunk row. Used by :func:`_process_file` (#53 cleanup).

    Upsert on ``(document_id, chunk_index)``: a content edit replaces
    the row in place so the chunk's UUID is stable. That stable ID is
    what ``chunk_embeddings`` references; the embedding pass will
    notice the ``content_hash`` drift and re-embed.
    """
    table = DocumentChunk.__table__
    chunk_extra = _chunk_security_metadata(
        chunk.content,
        summary,
        prompt_injection_phrases=prompt_injection_phrases,
        sensitive_patterns=sensitive_patterns,
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
    sensitive_patterns: list[Any] | None = None,
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
            sensitive_patterns=sensitive_patterns,
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
