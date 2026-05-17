"""Operational repositories: ingestion runs, queries, feedback, context packs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update

from cf_knowledge_kiln.db.models import (
    ContextPack,
    IngestionJob,
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


class IngestionJobsRepository(BaseRepository):
    """Queue repository for the ingestion worker.

    ``claim_one`` uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple
    workers polling the same queue never double-process a row.
    """

    async def create(
        self,
        *,
        source_id: UUID | None = None,
        kind: str = "full_resync",
        payload: dict[str, Any] | None = None,
    ) -> IngestionJob:
        row = IngestionJob(source_id=source_id, kind=kind, payload=payload or {})
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> IngestionJob | None:
        return await self._session.get(IngestionJob, id)

    async def list(
        self,
        *,
        status: str | None = None,
        source_id: UUID | None = None,
        limit: int | None = None,
    ) -> Sequence[IngestionJob]:
        stmt = select(IngestionJob)
        if status is not None:
            stmt = stmt.where(IngestionJob.status == status)
        if source_id is not None:
            stmt = stmt.where(IngestionJob.source_id == source_id)
        stmt = stmt.order_by(IngestionJob.enqueued_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(IngestionJob).where(IngestionJob.id == id))

    async def claim_one(self) -> IngestionJob | None:
        """Atomically claim the oldest queued job for this worker.

        Uses ``FOR UPDATE SKIP LOCKED`` to ensure two workers polling
        simultaneously each get a different row (or None). The claimed
        row transitions to ``running`` with ``started_at = now()``.
        Returns ``None`` if the queue is empty.
        """
        stmt = (
            select(IngestionJob)
            .where(IngestionJob.status == "queued")
            .order_by(IngestionJob.enqueued_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self._session.execute(stmt)).scalar_one_or_none()
        if job is None:
            return None
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job.id)
            .values(status="running", started_at=func.now(), attempts=IngestionJob.attempts + 1)
        )
        await self._session.flush()
        await self._session.refresh(job)
        return job

    async def mark_done(self, id: UUID, *, result_run_id: UUID | None = None) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == id)
            .values(status="succeeded", finished_at=func.now(), result_run_id=result_run_id)
        )

    async def mark_failed(self, id: UUID, *, error: str) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == id)
            .values(status="failed", finished_at=func.now(), last_error=error)
        )

    async def requeue(self, id: UUID) -> None:
        """Reset a failed job back to ``queued`` so it can be retried."""
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == id)
            .values(status="queued", started_at=None, finished_at=None, last_error=None)
        )
