"""Bulk re-embed helper (#224).

Operator-facing tool: walk every ``document_chunks`` row, re-embed
the content via the active provider's ``embed_documents`` (which
applies any model-family prefix — #216), and upsert back into
``chunk_embeddings``. The existing ``EmbeddingsRepository.upsert``
overwrites the row in place, so there's no DELETE first; this is
strictly safer than the previous manual ``TRUNCATE chunk_embeddings;
make ingest`` recipe documented post-#216.

Use cases:

* After bumping the embedding model in ``config/models.yaml`` —
  e.g. swapping ``e5-small-v2`` for ``nomic-embed-text-v1.5``.
* After landing a prefix-handling fix like #216 — chunks embedded
  without the prefix don't compose well against prefix-applied
  queries, and a re-embed restores the calibrated cosine range.
* After bumping ``sentence-transformers`` or ``torch`` floors with
  a calibration check (#222).

Called from the ``reembed`` CLI subcommand. The function is
independent of the source-allowlist polling that drives the rest of
the ingestion pipeline — it operates on whatever chunks already
exist in the DB, so a stuck source or empty source file doesn't
affect it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.db.models import DocumentChunk
from cf_knowledge_kiln.db.repositories import EmbeddingsRepository
from cf_knowledge_kiln.ingestion.embedding.batched import embed_chunks_concurrently

if TYPE_CHECKING:
    from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReembedResult:
    """What :func:`reembed_all_chunks` reports back.

    * ``chunks_total`` — every row in ``document_chunks``. Equals
      ``chunks_embedded + chunks_failed`` when ``dry_run`` is False.
    * ``chunks_embedded`` — upserts that succeeded.
    * ``chunks_failed`` — chunks in batches that raised. The per-batch
      exception is logged at WARNING for forensics; the helper does
      NOT raise unless EVERY batch failed (matches the existing
      ``embed_touched_documents`` semantics — partial-success is
      preferable to losing all the good vectors over one bad batch).
    * ``failures`` — per-batch ``(start_offset, batch_size, exc)``
      tuples for the report.
    """

    chunks_total: int
    chunks_embedded: int
    chunks_failed: int
    failures: list[tuple[int, int, BaseException]]


async def reembed_all_chunks(
    session: AsyncSession,
    *,
    provider: EmbeddingProvider,
    batch_size: int = 32,
    concurrency: int = 4,
    dry_run: bool = False,
) -> ReembedResult:
    """Re-embed every chunk in ``document_chunks``.

    Unlike :func:`embed_touched_documents`, this helper does NOT consult
    the existing-hashes map — every chunk gets re-embedded, regardless
    of whether the current embedding row already matches. That's the
    whole point: the existing rows are the ones we want to replace
    (different model, or different prefix per #216).

    ``dry_run`` returns the chunk count without calling the provider
    or writing anything — operator preview.

    Raises only when EVERY batch fails (the helper's "loud failure"
    contract). Partial-success runs return a populated
    :class:`ReembedResult` with the ``failures`` list intact.
    """
    chunks = await _load_all_chunks(session)
    total = len(chunks)
    if total == 0 or dry_run:
        return ReembedResult(
            chunks_total=total,
            chunks_embedded=0,
            chunks_failed=0,
            failures=[],
        )

    logger.info("reembed: %d chunks via %s/%s", total, provider.provider, provider.model)
    result = await embed_chunks_concurrently(
        texts=[c.content for c in chunks],
        provider=provider,
        batch_size=batch_size,
        concurrency=concurrency,
    )

    # Log per-batch forensics (same shape as embed_touched_documents
    # so an operator reading both logs sees one format).
    for start, size, exc in result.failures:
        logger.warning(
            "reembed batch failed",
            extra={
                "start_offset": start,
                "batch_size": size,
                "exc_class": type(exc).__name__,
                "exc_message": str(exc),
            },
        )

    # "All batches failed" — mirror embed_touched_documents and raise.
    if result.failures and all(v is None for v in result.vectors):
        _, _, first_exc = result.failures[0]
        raise first_exc

    embeds_repo = EmbeddingsRepository(session)
    embedded, failed = await _write_reembed_vectors(
        chunks=chunks,
        vectors=result.vectors,
        repo=embeds_repo,
        provider=provider,
    )
    logger.info(
        "reembed complete: %d embedded, %d failed, %d total",
        embedded,
        failed,
        total,
    )
    return ReembedResult(
        chunks_total=total,
        chunks_embedded=embedded,
        chunks_failed=failed,
        failures=result.failures,
    )


async def _load_all_chunks(session: AsyncSession) -> list[DocumentChunk]:
    """Return every chunk, ordered for determinism (and progress reporting).

    Stable ordering: document_id then chunk_index. Real corpora aren't
    huge (the project's ``ingest_max_files`` cap keeps total chunks in
    the thousands, well under what an in-memory list strains), so a
    cursor isn't worth the complexity yet.
    """
    rows = (
        (
            await session.execute(
                select(DocumentChunk).order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _write_reembed_vectors(
    *,
    chunks: list[DocumentChunk],
    vectors: list[list[float] | None],
    repo: EmbeddingsRepository,
    provider: EmbeddingProvider,
) -> tuple[int, int]:
    """Upsert ``(chunk, vector)`` pairs. Returns ``(embedded, failed)``.

    Mirrors :func:`_write_embeddings` in ``embedding/pipeline.py`` —
    we don't share the function because that helper writes into a
    ``summary`` mutable; the re-embed CLI doesn't have a summary
    object and returning the counts is cleaner.
    """
    embedded = 0
    failed = 0
    for chunk, vector in zip(chunks, vectors, strict=True):
        if vector is None:
            failed += 1
            continue
        await repo.upsert(
            chunk_id=chunk.id,
            embedding=vector,
            model=provider.model,
            provider=provider.provider,
            dimensions=provider.dimensions,
            content_hash=chunk.content_hash,
        )
        embedded += 1
    return embedded, failed


__all__ = ["ReembedResult", "reembed_all_chunks"]
