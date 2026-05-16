"""Operational repositories: ingestion runs, queries, feedback, context packs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from cf_knowledge_kiln.db.models import (
    ContextPack,
    IngestionRun,
    RagFeedback,
    RagQuery,
)
from cf_knowledge_kiln.db.repositories._base import BaseRepository


class IngestionRunsRepository(BaseRepository):
    async def create(
        self,
        *,
        source_id: UUID | None = None,
        status: str = "running",
        stats: dict[str, Any] | None = None,
        commit_sha: str | None = None,
    ) -> IngestionRun:
        row = IngestionRun(
            source_id=source_id,
            status=status,
            stats=stats or {},
            commit_sha=commit_sha,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> IngestionRun | None:
        return await self._session.get(IngestionRun, id)

    async def list(
        self,
        *,
        source_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> Sequence[IngestionRun]:
        stmt = select(IngestionRun)
        if source_id is not None:
            stmt = stmt.where(IngestionRun.source_id == source_id)
        if status is not None:
            stmt = stmt.where(IngestionRun.status == status)
        stmt = stmt.order_by(IngestionRun.started_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(IngestionRun).where(IngestionRun.id == id))


class QueriesRepository(BaseRepository):
    async def create(
        self,
        *,
        query: str,
        consumer_type: str,
        requester: str | None = None,
        filters: dict[str, Any] | None = None,
        retrieved_chunk_ids: Sequence[UUID] | None = None,
    ) -> RagQuery:
        row = RagQuery(
            query=query,
            consumer_type=consumer_type,
            requester=requester,
            filters=filters or {},
            retrieved_chunk_ids=list(retrieved_chunk_ids or []),
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> RagQuery | None:
        return await self._session.get(RagQuery, id)

    async def list(
        self,
        *,
        consumer_type: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[RagQuery]:
        stmt = select(RagQuery)
        if consumer_type is not None:
            stmt = stmt.where(RagQuery.consumer_type == consumer_type)
        if since is not None:
            stmt = stmt.where(RagQuery.created_at >= since)
        stmt = stmt.order_by(RagQuery.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(RagQuery).where(RagQuery.id == id))


class FeedbackRepository(BaseRepository):
    async def create(
        self,
        *,
        signal: str,
        query_id: UUID | None = None,
        chunk_id: UUID | None = None,
        comment: str | None = None,
        source: str | None = None,
    ) -> RagFeedback:
        row = RagFeedback(
            signal=signal,
            query_id=query_id,
            chunk_id=chunk_id,
            comment=comment,
            source=source,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> RagFeedback | None:
        return await self._session.get(RagFeedback, id)

    async def list(
        self,
        *,
        chunk_id: UUID | None = None,
        query_id: UUID | None = None,
        signal: str | None = None,
        limit: int | None = None,
    ) -> Sequence[RagFeedback]:
        stmt = select(RagFeedback)
        if chunk_id is not None:
            stmt = stmt.where(RagFeedback.chunk_id == chunk_id)
        if query_id is not None:
            stmt = stmt.where(RagFeedback.query_id == query_id)
        if signal is not None:
            stmt = stmt.where(RagFeedback.signal == signal)
        stmt = stmt.order_by(RagFeedback.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(RagFeedback).where(RagFeedback.id == id))


class ContextPacksRepository(BaseRepository):
    async def create(
        self,
        *,
        query: str,
        task: str,
        token_budget: int,
        filters: dict[str, Any] | None = None,
        evidence_chunk_ids: Sequence[UUID] | None = None,
        token_estimate: int | None = None,
        confidence: str | None = None,
        warnings: list[Any] | None = None,
        requires_human_review: bool = False,
    ) -> ContextPack:
        row = ContextPack(
            query=query,
            task=task,
            token_budget=token_budget,
            filters=filters or {},
            evidence_chunk_ids=list(evidence_chunk_ids or []),
            token_estimate=token_estimate,
            confidence=confidence,
            warnings=warnings or [],
            requires_human_review=requires_human_review,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> ContextPack | None:
        return await self._session.get(ContextPack, id)

    async def list(
        self,
        *,
        requires_human_review: bool | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[ContextPack]:
        stmt = select(ContextPack)
        if requires_human_review is not None:
            stmt = stmt.where(ContextPack.requires_human_review.is_(requires_human_review))
        if since is not None:
            stmt = stmt.where(ContextPack.created_at >= since)
        stmt = stmt.order_by(ContextPack.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(ContextPack).where(ContextPack.id == id))
