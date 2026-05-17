"""Embedding pass for the ingestion pipeline.

Lives here (and not in :mod:`cf_knowledge_kiln.ingestion.pipeline`) so
the per-file file-size guideline holds; the document/chunk pass and
the embedding pass are independent enough that the split also makes
the call site in ``pipeline.run_source`` easier to read.

The single public entrypoint is :func:`embed_touched_documents`.
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

if TYPE_CHECKING:
    from cf_knowledge_kiln.ingestion.pipeline import IngestionSummary

logger = logging.getLogger(__name__)


async def embed_touched_documents(
    session: AsyncSession,
    *,
    doc_ids: set[UUID],
    provider: EmbeddingProvider,
    summary: IngestionSummary,
) -> None:
    """Embed any chunk in ``doc_ids`` whose vector is missing or stale.

    Issue #18: zero embedding-provider calls when content hasn't moved.
    The gate is the per-chunk ``content_hash`` on the embedding row —
    if it equals the chunk's current hash, the existing vector is
    still correct.

    Failures on a single document are recorded but don't abort the run
    — partial embedding coverage is still useful for retrieval, and
    bouncing the whole run wastes the chunk-pass work that already
    succeeded.
    """
    if not doc_ids:
        return
    # Flush so newly-inserted chunks have IDs the embedding pass can use.
    await session.flush()
    embeds_repo = EmbeddingsRepository(session)
    for doc_id in doc_ids:
        await _embed_one_document(
            session, doc_id=doc_id, repo=embeds_repo, provider=provider, summary=summary
        )


async def _embed_one_document(
    session: AsyncSession,
    *,
    doc_id: UUID,
    repo: EmbeddingsRepository,
    provider: EmbeddingProvider,
    summary: IngestionSummary,
) -> None:
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
        return
    existing = await repo.existing_hashes_for_document(doc_id)
    to_embed = [c for c in chunks_for_doc if existing.get(c.id) != c.content_hash]
    summary.embeddings_unchanged += len(chunks_for_doc) - len(to_embed)
    if not to_embed:
        return
    try:
        vectors = await provider.embed([c.content for c in to_embed])
    except Exception as exc:
        summary.errors.append(f"embedding failed for document {doc_id}: {exc}")
        summary.embeddings_failed += len(to_embed)
        logger.warning("embedding pass failed for document %s: %s", doc_id, exc)
        return
    if len(vectors) != len(to_embed):
        summary.errors.append(
            f"embedding provider returned {len(vectors)} vectors for "
            f"{len(to_embed)} chunks in document {doc_id}"
        )
        summary.embeddings_failed += len(to_embed)
        return
    for chunk, vector in zip(to_embed, vectors, strict=True):
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
