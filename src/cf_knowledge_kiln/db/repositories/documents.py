"""Repositories for documents, chunks, and chunk embeddings.

Phase 5 hybrid retrieval (slice 2): :meth:`ChunksRepository.hybrid_search`
issues a single CTE per ADR-0009 §5 — vector arm + FTS arm rank-fused
via Reciprocal Rank Fusion (k=60), with metadata filters pushed into
both arms before the union. :meth:`ChunksRepository.search_by_fts` is
the FTS-only fallback used when no embedding provider is configured.
The SQL builders live in :mod:`._hybrid` to keep this file under the
400-line budget.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cf_knowledge_kiln.db.models import ChunkEmbedding, Document, DocumentChunk
from cf_knowledge_kiln.db.repositories._base import BaseRepository, apply_eq_filters
from cf_knowledge_kiln.db.repositories._hybrid import (
    SearchRow,
    build_fts_only_select,
    build_hybrid_select,
    row_to_search_row,
    set_local_ef_search,
)

if TYPE_CHECKING:
    # Avoid an import cycle: ``retrieval/__init__.py`` exports
    # HybridRetriever, which imports ChunksRepository from this
    # module. Annotations are strings (``from __future__ import
    # annotations``); the actual build_predicates call is lazy.
    from cf_knowledge_kiln.retrieval.types import RetrievalFilters


class DocumentsRepository(BaseRepository):
    async def create(
        self,
        *,
        repo: str,
        path: str,
        title: str,
        doc_type: str | None = None,
        status: str = "active",
        owner: str | None = None,
        system: str | None = None,
        authority: str | None = None,
        sensitivity: str | None = None,
        source_url: str | None = None,
        commit_sha: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        return await self._persist(
            Document(
                repo=repo,
                path=path,
                title=title,
                doc_type=doc_type,
                status=status,
                owner=owner,
                system=system,
                authority=authority,
                sensitivity=sensitivity,
                source_url=source_url,
                commit_sha=commit_sha,
                extra=metadata or {},
            )
        )

    async def get(self, id: UUID) -> Document | None:
        return await self._session.get(Document, id)

    async def list(
        self,
        *,
        status: str | None = None,
        repo: str | None = None,
        doc_type: str | None = None,
        limit: int | None = None,
    ) -> Sequence[Document]:
        stmt = apply_eq_filters(
            select(Document),
            {Document.status: status, Document.repo: repo, Document.doc_type: doc_type},
        ).order_by(Document.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(Document).where(Document.id == id))


class ChunksRepository(BaseRepository):
    async def create(
        self,
        *,
        document_id: UUID,
        chunk_index: int,
        content: str,
        content_hash: str,
        heading_path: list[str] | None = None,
        content_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentChunk:
        return await self._persist(
            DocumentChunk(
                document_id=document_id,
                chunk_index=chunk_index,
                content=content,
                content_hash=content_hash,
                heading_path=heading_path or [],
                content_tokens=content_tokens,
                extra=metadata or {},
            )
        )

    async def get(self, id: UUID) -> DocumentChunk | None:
        return await self._session.get(DocumentChunk, id)

    async def list(
        self,
        *,
        document_id: UUID | None = None,
        content_hash: str | None = None,
        limit: int | None = None,
    ) -> Sequence[DocumentChunk]:
        stmt = apply_eq_filters(
            select(DocumentChunk),
            {
                DocumentChunk.document_id: document_id,
                DocumentChunk.content_hash: content_hash,
            },
        ).order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(DocumentChunk).where(DocumentChunk.id == id))

    async def neighbors(
        self, chunk_id: UUID, *, n: int = 1
    ) -> tuple[Sequence[DocumentChunk], DocumentChunk | None, Sequence[DocumentChunk]]:
        """Return ``(prev, target, next)`` chunks for the document-preview panel.

        ``prev`` and ``next`` are ordered ascending by ``chunk_index`` and
        may contain fewer than ``n`` entries near document boundaries.
        Returns ``([], None, [])`` when the target chunk does not exist.
        """
        target = await self._session.get(DocumentChunk, chunk_id)
        if target is None:
            return ([], None, [])
        prev_stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == target.document_id,
                DocumentChunk.chunk_index < target.chunk_index,
            )
            .order_by(DocumentChunk.chunk_index.desc())
            .limit(n)
        )
        next_stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == target.document_id,
                DocumentChunk.chunk_index > target.chunk_index,
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(n)
        )
        prev_rows = [*((await self._session.execute(prev_stmt)).scalars().all())]
        prev_rows.reverse()
        next_rows = [*((await self._session.execute(next_stmt)).scalars().all())]
        return (prev_rows, target, next_rows)

    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_embedding: Sequence[float],
        dimensions: int,
        filters: RetrievalFilters,
        top_per_arm: int = 100,
        final_limit: int = 20,
        rrf_k: int = 60,
        ef_search: int = 200,
    ) -> Sequence[SearchRow]:
        """Hybrid pgvector + FTS search fused via RRF in a single CTE.

        Implements ADR-0009 §5. Both arms apply the same metadata
        predicates from :func:`build_predicates`. ``SET LOCAL
        hnsw.ef_search`` is issued so the vector arm hits the recall
        target without leaking the setting beyond this transaction.
        """
        from cf_knowledge_kiln.retrieval.filters import build_predicates

        await set_local_ef_search(self._session, ef_search)
        predicates = build_predicates(filters)
        stmt = build_hybrid_select(
            query_text=query_text,
            query_embedding=query_embedding,
            dimensions=dimensions,
            predicates=predicates,
            top_per_arm=top_per_arm,
            final_limit=final_limit,
            rrf_k=rrf_k,
        )
        result = await self._session.execute(stmt)
        return [row_to_search_row(row) for row in result.mappings()]

    async def search_by_fts(
        self,
        *,
        query_text: str,
        filters: RetrievalFilters,
        limit: int = 100,
    ) -> Sequence[SearchRow]:
        """FTS-only fallback for when no embedding provider is wired.

        Same row shape as :meth:`hybrid_search`; ``score`` is
        ``ts_rank_cd`` (unbounded but positive, so the boost
        multipliers in retrieval/ranking apply without renormalization).
        """
        from cf_knowledge_kiln.retrieval.filters import build_predicates

        stmt = build_fts_only_select(
            query_text=query_text,
            predicates=build_predicates(filters),
            limit=limit,
        )
        result = await self._session.execute(stmt)
        return [row_to_search_row(row) for row in result.mappings()]


class EmbeddingsRepository(BaseRepository):
    async def create(
        self,
        *,
        chunk_id: UUID,
        embedding: Sequence[float],
        model: str,
        provider: str,
        dimensions: int,
        content_hash: str,
    ) -> ChunkEmbedding:
        return await self._persist(
            ChunkEmbedding(
                chunk_id=chunk_id,
                embedding=list(embedding),
                model=model,
                provider=provider,
                dimensions=dimensions,
                content_hash=content_hash,
            )
        )

    async def get(self, id: UUID) -> ChunkEmbedding | None:
        """Get by chunk_id (the table's primary key)."""
        return await self._session.get(ChunkEmbedding, id)

    async def list(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        dimensions: int | None = None,
        limit: int | None = None,
    ) -> Sequence[ChunkEmbedding]:
        stmt = apply_eq_filters(
            select(ChunkEmbedding),
            {
                ChunkEmbedding.model: model,
                ChunkEmbedding.provider: provider,
                ChunkEmbedding.dimensions: dimensions,
            },
        ).order_by(ChunkEmbedding.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id == id))

    async def upsert(
        self,
        *,
        chunk_id: UUID,
        embedding: Sequence[float],
        model: str,
        provider: str,
        dimensions: int,
        content_hash: str,
    ) -> None:
        """Insert or overwrite the row for ``chunk_id``.

        ``chunk_embeddings`` has ``chunk_id`` as its sole primary key:
        one embedding per chunk, intentionally. Swapping models =
        reindex (Phase 4 plan). Re-embedding the same chunk after a
        content edit replaces the vector and updates ``content_hash``.
        """
        stmt = pg_insert(ChunkEmbedding).values(
            chunk_id=chunk_id,
            embedding=list(embedding),
            model=model,
            provider=provider,
            dimensions=dimensions,
            content_hash=content_hash,
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["chunk_id"],
                set_={
                    "embedding": stmt.excluded.embedding,
                    "model": stmt.excluded.model,
                    "provider": stmt.excluded.provider,
                    "dimensions": stmt.excluded.dimensions,
                    "content_hash": stmt.excluded.content_hash,
                },
            )
        )

    async def existing_hashes_for_document(self, document_id: UUID) -> dict[UUID, str]:
        """Return ``{chunk_id: content_hash}`` for chunks already embedded.

        Used by the ingestion pipeline to skip re-embedding chunks whose
        content hasn't changed (issue #18). A chunk that isn't in the
        result map either has no embedding yet, or its row was deleted.
        """
        stmt = (
            select(DocumentChunk.id, ChunkEmbedding.content_hash)
            .join(ChunkEmbedding, ChunkEmbedding.chunk_id == DocumentChunk.id)
            .where(DocumentChunk.document_id == document_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}


__all__ = [
    "ChunksRepository",
    "DocumentsRepository",
    "EmbeddingsRepository",
    "SearchRow",
]
