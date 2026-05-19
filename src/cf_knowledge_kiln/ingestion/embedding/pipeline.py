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

    Failures on the embedding step are recorded against the affected
    chunks but don't abort the run — partial embedding coverage is
    still useful for retrieval, and bouncing the whole run wastes the
    chunk-pass work that already succeeded.
    """
    if not doc_ids:
        return
    # Flush so newly-inserted chunks have IDs the embedding pass can use.
    await session.flush()
    embeds_repo = EmbeddingsRepository(session)
    to_embed = await _gather_chunks_needing_embed(
        session, doc_ids=doc_ids, repo=embeds_repo, summary=summary
    )
    if not to_embed:
        return
    try:
        vectors = await embed_chunks_concurrently(
            texts=[c.content for c in to_embed],
            provider=provider,
            batch_size=batch_size,
            concurrency=concurrency,
        )
    except Exception as exc:
        summary.errors.append(f"embedding fan-out failed: {exc}")
        summary.embeddings_failed += len(to_embed)
        logger.warning("embedding fan-out failed (%d chunks): %s", len(to_embed), exc)
        return
    await _write_embeddings(
        chunks=to_embed,
        vectors=vectors,
        repo=embeds_repo,
        provider=provider,
        summary=summary,
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
    vectors: list[list[float]],
    repo: EmbeddingsRepository,
    provider: EmbeddingProvider,
    summary: IngestionSummary,
) -> None:
    """Upsert ``(chunk, vector)`` pairs. Order-paired 1:1 with ``chunks``."""
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
