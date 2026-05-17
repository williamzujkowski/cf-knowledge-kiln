"""Repositories for documents, chunks, and chunk embeddings."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cf_knowledge_kiln.db.models import ChunkEmbedding, Document, DocumentChunk
from cf_knowledge_kiln.db.repositories._base import BaseRepository


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
        row = Document(
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
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

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
        stmt = select(Document)
        if status is not None:
            stmt = stmt.where(Document.status == status)
        if repo is not None:
            stmt = stmt.where(Document.repo == repo)
        if doc_type is not None:
            stmt = stmt.where(Document.doc_type == doc_type)
        stmt = stmt.order_by(Document.created_at.desc())
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
        row = DocumentChunk(
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            content_hash=content_hash,
            heading_path=heading_path or [],
            content_tokens=content_tokens,
            extra=metadata or {},
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> DocumentChunk | None:
        return await self._session.get(DocumentChunk, id)

    async def list(
        self,
        *,
        document_id: UUID | None = None,
        content_hash: str | None = None,
        limit: int | None = None,
    ) -> Sequence[DocumentChunk]:
        stmt = select(DocumentChunk)
        if document_id is not None:
            stmt = stmt.where(DocumentChunk.document_id == document_id)
        if content_hash is not None:
            stmt = stmt.where(DocumentChunk.content_hash == content_hash)
        stmt = stmt.order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(DocumentChunk).where(DocumentChunk.id == id))


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
        row = ChunkEmbedding(
            chunk_id=chunk_id,
            embedding=list(embedding),
            model=model,
            provider=provider,
            dimensions=dimensions,
            content_hash=content_hash,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

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
        stmt = select(ChunkEmbedding)
        if model is not None:
            stmt = stmt.where(ChunkEmbedding.model == model)
        if provider is not None:
            stmt = stmt.where(ChunkEmbedding.provider == provider)
        if dimensions is not None:
            stmt = stmt.where(ChunkEmbedding.dimensions == dimensions)
        stmt = stmt.order_by(ChunkEmbedding.created_at.desc())
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
