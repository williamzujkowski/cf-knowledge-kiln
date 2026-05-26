"""Embedding pass for the ingestion pipeline.

Lives here (and not in :mod:`cf_knowledge_kiln.ingestion.pipeline`) so
the per-file file-size guideline holds; the document/chunk pass and
the embedding pass are independent enough that the split also makes
the call site in ``pipeline.run_source`` easier to read.

The single public entrypoint is :func:`embed_touched_documents`.

Concurrency model (PR C, prep for #108 item 2):

* Per document, we still consult :meth:`EmbeddingsRepository
  .existing_hashes_for_document` so chunks with a fresh embedding
  are skipped — issue #18.
* The chunks that DO need embedding (across all touched docs) are
  fed through :func:`embed_chunks_concurrently`, which slices them
  into batches of ``batch_size`` and runs up to ``concurrency``
  batches in parallel. Per-batch failures are recorded against the
  documents whose chunks were in that batch; the run survives.
* Output order is preserved by the fan-out, so the upsert phase
  writes ``chunk_id → vector`` pairs without any re-sorting.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.db.models import DocumentChunk
from cf_knowledge_kiln.db.repositories import EmbeddingsRepository
from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider
from cf_knowledge_kiln.ingestion.embedding.batched import embed_chunks_concurrently

if TYPE_CHECKING:
    from cf_knowledge_kiln.ingestion.pipeline import IngestionSummary

logger = logging.getLogger(__name__)


async def embed_touched_documents(
    session: AsyncSession,
    *,
    doc_ids: set[UUID],
    provider: EmbeddingProvider,
    summary: IngestionSummary,
    batch_size: int = 32,
    concurrency: int = 4,
) -> None:
    """Embed any chunk in ``doc_ids`` whose vector is missing or stale.

    Issue #18: zero embedding-provider calls when content hasn't moved.
    The gate is the per-chunk ``content_hash`` on the embedding row —
    if it equals the chunk's current hash, the existing vector is
    still correct.

    Batched concurrent fan-out: the chunks that DO need embedding are
    sliced into batches of ``batch_size`` and up to ``concurrency``
    batches are in flight at once. Defaults match
    :class:`cf_knowledge_kiln.config.Settings`.

    Per-batch failure granularity (#151): a transient provider error
    on one batch no longer discards the sibling batches' successful
    vectors. Successful batches are persisted; the failed batch's
    chunks are accounted as ``embeddings_failed`` and the per-batch
    exception is logged with ``start_offset``, ``batch_size``,
    ``exc_class``, ``exc_message`` for forensics. The function only
    raises (propagating the exception) when EVERY batch failed — at
    that point there is no partial success worth committing and the
    caller's "loud failure" semantics still hold.
    """
    if not doc_ids:
        return
    # Flush so newly-inserted chunks have IDs the embedding pass can use.
    await session.flush()
    embeds_repo = EmbeddingsRepository(session)
    to_embed = await _gather_chunks_needing_embed(
        session, doc_ids=doc_ids, repo=embeds_repo, summary=summary
    )
    # #286: commit the dedup-check tx BEFORE the embed phase. The two
    # SELECTs inside ``_gather_chunks_needing_embed`` (the per-document
    # chunk fetch and the chunks-JOIN-chunk_embeddings hash lookup) run
    # under SQLAlchemy's autobegin: the session opens a transaction on
    # the first SELECT and holds it until an explicit commit/rollback.
    # The embed phase that follows is minutes of CPU-bound model work
    # in an executor thread (no DB touches), so without this commit the
    # connection sits ``idle in transaction`` for the entire embed
    # duration. pg_stat_activity then shows one async-pool connection
    # stuck on the JOIN SELECT as its last query, two siblings idle
    # post-COMMIT, and the worker silent — the fingerprint reported in
    # #286 (regression of the same pattern #245 closed for the
    # data_sources lock site; that fix stays in place, this one closes
    # the parallel leak at the dedup-check call site).
    #
    # _write_embeddings below opens a fresh transaction implicitly on
    # its first upsert; the run_source caller's final commit then
    # closes it as before.
    await session.commit()
    if not to_embed:
        return
    result = await embed_chunks_concurrently(
        texts=[c.content for c in to_embed],
        provider=provider,
        batch_size=batch_size,
        concurrency=concurrency,
    )
    _record_batch_failures(result.failures, summary=summary)
    if result.failures and all(v is None for v in result.vectors):
        # Every batch failed: re-raise the first exception so the run's
        # "loud failure" semantics hold for hopeless cases. The summary
        # already has the per-batch error breadcrumbs from above.
        _, _, first_exc = result.failures[0]
        raise first_exc
    await _write_embeddings(
        chunks=to_embed,
        vectors=result.vectors,
        repo=embeds_repo,
        provider=provider,
        summary=summary,
    )


def _record_batch_failures(
    failures: list[tuple[int, int, BaseException]],
    *,
    summary: IngestionSummary,
) -> None:
    """Log per-batch forensics and account each failed chunk in the summary.

    One structured log line per failed batch (start_offset, batch_size,
    exc_class, exc_message — no full traceback, by design: production
    logs stay readable). The summary picks up ``embeddings_failed`` per
    chunk in the failed batch plus a per-batch error string.
    """
    for start, size, exc in failures:
        summary.embeddings_failed += size
        msg = f"embedding batch failed (offset={start}, size={size}): {exc}"
        summary.errors.append(msg)
        logger.warning(
            "embedding batch failed",
            extra={
                "start_offset": start,
                "batch_size": size,
                "exc_class": type(exc).__name__,
                "exc_message": str(exc),
            },
        )


async def _gather_chunks_needing_embed(
    session: AsyncSession,
    *,
    doc_ids: set[UUID],
    repo: EmbeddingsRepository,
    summary: IngestionSummary,
) -> list[DocumentChunk]:
    """Walk every touched document; collect chunks whose hash drifted.

    Per-doc loop preserves the existing skip-already-embedded
    semantics (#18). The combined result is what feeds the
    concurrent fan-out — so we get cross-doc parallelism without
    losing per-doc hash-skip accounting.
    """
    needs_embed: list[DocumentChunk] = []
    for doc_id in doc_ids:
        chunks_for_doc = (
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            )
            .scalars()
            .all()
        )
        if not chunks_for_doc:
            continue
        existing = await repo.existing_hashes_for_document(doc_id)
        fresh = [c for c in chunks_for_doc if existing.get(c.id) != c.content_hash]
        summary.embeddings_unchanged += len(chunks_for_doc) - len(fresh)
        needs_embed.extend(fresh)
    return needs_embed


async def _write_embeddings(
    *,
    chunks: list[DocumentChunk],
    vectors: list[list[float] | None],
    repo: EmbeddingsRepository,
    provider: EmbeddingProvider,
    summary: IngestionSummary,
) -> None:
    """Upsert ``(chunk, vector)`` pairs. Order-paired 1:1 with ``chunks``.

    ``vectors`` may contain ``None`` entries for chunks whose batch
    failed (#151). Those slots are skipped here — the failure was
    already accounted in ``embeddings_failed`` by
    :func:`_record_batch_failures`. Only non-None slots produce an
    upsert + ``embeddings_created`` increment.
    """
    if len(vectors) != len(chunks):
        # The fan-out helper already enforces this, but defend the
        # write path too — the embeddings table treats chunk_id as
        # PK so a misaligned pairing would silently scramble data.
        summary.errors.append(
            f"embedding fan-out returned {len(vectors)} vectors for {len(chunks)} chunks"
        )
        summary.embeddings_failed += len(chunks)
        return
    for chunk, vector in zip(chunks, vectors, strict=True):
        if vector is None:
            # Already accounted as a per-batch failure upstream.
            continue
        await repo.upsert(
            chunk_id=chunk.id,
            embedding=vector,
            model=provider.model,
            provider=provider.provider,
            dimensions=provider.dimensions,
            content_hash=chunk.content_hash,
        )
        summary.embeddings_created += 1


__all__ = ["embed_touched_documents"]
