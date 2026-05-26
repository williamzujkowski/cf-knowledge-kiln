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
    RagAnswer,
    RagFeedback,
    RagQuery,
)
from cf_knowledge_kiln.db.repositories._base import BaseRepository, apply_eq_filters


class IngestionRunsRepository(BaseRepository):
    async def create(
        self,
        *,
        source_id: UUID | None = None,
        status: str = "running",
        stats: dict[str, Any] | None = None,
        commit_sha: str | None = None,
    ) -> IngestionRun:
        return await self._persist(
            IngestionRun(
                source_id=source_id,
                status=status,
                stats=stats or {},
                commit_sha=commit_sha,
            )
        )

    async def get(self, id: UUID) -> IngestionRun | None:
        return await self._session.get(IngestionRun, id)

    async def list(
        self,
        *,
        source_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> Sequence[IngestionRun]:
        stmt = apply_eq_filters(
            select(IngestionRun),
            {IngestionRun.source_id: source_id, IngestionRun.status: status},
        ).order_by(IngestionRun.started_at.desc())
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
        request_id: str | None = None,
    ) -> RagQuery:
        """Insert one ``rag_queries`` row.

        ``request_id`` (#260): the X-Request-ID correlation key from
        the middleware. Optional so a bare test harness without the
        middleware (or a pre-migration call site) still works; the
        column is nullable and a non-indexed null incurs no cost.
        """
        return await self._persist(
            RagQuery(
                query=query,
                consumer_type=consumer_type,
                requester=requester,
                filters=filters or {},
                retrieved_chunk_ids=list(retrieved_chunk_ids or []),
                request_id=request_id,
            )
        )

    async def get(self, id: UUID) -> RagQuery | None:
        return await self._session.get(RagQuery, id)

    async def list(
        self,
        *,
        consumer_type: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[RagQuery]:
        # `since` is a `>=` comparison, not eq, so handle it outside
        # apply_eq_filters.
        stmt = apply_eq_filters(select(RagQuery), {RagQuery.consumer_type: consumer_type})
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
        return await self._persist(
            RagFeedback(
                signal=signal,
                query_id=query_id,
                chunk_id=chunk_id,
                comment=comment,
                source=source,
            )
        )

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
        stmt = apply_eq_filters(
            select(RagFeedback),
            {
                RagFeedback.chunk_id: chunk_id,
                RagFeedback.query_id: query_id,
                RagFeedback.signal: signal,
            },
        ).order_by(RagFeedback.created_at.desc())
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
        id: UUID | None = None,
        request_id: str | None = None,
    ) -> ContextPack:
        """Insert one ``context_packs`` row.

        ``id`` (#256): when provided, the row's PK is set explicitly so it
        matches the response-visible ``context_pack_id`` an agent caller
        received. Without this, the audit row's PK is a fresh UUID
        unrelated to the wire value — an operator who gets a complaint
        quoting ``context_pack_id: abc...`` has no DB key to look it up.

        When ``id`` is None the previous behavior holds: the model's
        ``_pk()`` default fires and the row gets a fresh UUID. Tests
        and pre-#256 callers continue to work.

        ``request_id`` (#260): the X-Request-ID correlation key. With
        both ``id`` and ``request_id`` persisted, an operator handed
        a request_id from a user complaint can match the wire-visible
        context_pack_id back to this row and reconstruct the exact
        chunks the agent saw.
        """
        return await self._persist(
            ContextPack(
                **({"id": id} if id is not None else {}),
                query=query,
                task=task,
                token_budget=token_budget,
                filters=filters or {},
                evidence_chunk_ids=list(evidence_chunk_ids or []),
                token_estimate=token_estimate,
                confidence=confidence,
                warnings=warnings or [],
                requires_human_review=requires_human_review,
                request_id=request_id,
            )
        )

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
            # `is_(True/False)` rather than ``==`` because Pythonic
            # equality on Bool columns warns about identity vs eq.
            stmt = stmt.where(ContextPack.requires_human_review.is_(requires_human_review))
        if since is not None:
            stmt = stmt.where(ContextPack.created_at >= since)
        stmt = stmt.order_by(ContextPack.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(ContextPack).where(ContextPack.id == id))


class AnswersRepository(BaseRepository):
    """#221: per-request telemetry for ``POST /v1/answer``.

    Mirrors the other repositories' shape (``create`` / ``get`` /
    ``list`` / ``delete``). The route writes one row per request via
    ``create`` inside a SAVEPOINT so a transient DB failure doesn't
    cascade to a 500.
    """

    async def create(
        self,
        *,
        query: str,
        answerable: bool,
        requires_human_review: bool,
        requested_max_answer_tokens: int,
        task: str | None = None,
        filters: dict[str, Any] | None = None,
        evidence_chunk_ids: Sequence[UUID] | None = None,
        refusal_reason: str | None = None,
        confidence: str | None = None,
        generator_provider: str | None = None,
        generator_model: str | None = None,
        finish_reason: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        id: UUID | None = None,
        request_id: str | None = None,
    ) -> RagAnswer:
        """Insert one ``rag_answers`` row.

        ``id`` (#256): when provided, sets the row's PK explicitly so
        it matches the response-visible ``answer_id``. See the
        ContextPacksRepository.create docstring for the audit-gap
        rationale this closes.

        ``request_id`` (#260): the X-Request-ID correlation key from
        the middleware. With both ``id`` and ``request_id`` persisted,
        an operator handed a request_id from a user complaint can
        match the wire-visible answer_id back to this row, see the
        evidence chunks, and reconstruct what the generator saw.
        """
        return await self._persist(
            RagAnswer(
                **({"id": id} if id is not None else {}),
                query=query,
                task=task,
                filters=filters or {},
                evidence_chunk_ids=list(evidence_chunk_ids or []),
                answerable=answerable,
                requires_human_review=requires_human_review,
                refusal_reason=refusal_reason,
                confidence=confidence,
                generator_provider=generator_provider,
                generator_model=generator_model,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                requested_max_answer_tokens=requested_max_answer_tokens,
                request_id=request_id,
            )
        )

    async def get(self, id: UUID) -> RagAnswer | None:
        return await self._session.get(RagAnswer, id)

    async def list(
        self,
        *,
        answerable: bool | None = None,
        requires_human_review: bool | None = None,
        generator_model: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[RagAnswer]:
        stmt = select(RagAnswer)
        if answerable is not None:
            stmt = stmt.where(RagAnswer.answerable.is_(answerable))
        if requires_human_review is not None:
            stmt = stmt.where(RagAnswer.requires_human_review.is_(requires_human_review))
        if generator_model is not None:
            stmt = stmt.where(RagAnswer.generator_model == generator_model)
        if since is not None:
            stmt = stmt.where(RagAnswer.created_at >= since)
        stmt = stmt.order_by(RagAnswer.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(RagAnswer).where(RagAnswer.id == id))


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
        return await self._persist(
            IngestionJob(source_id=source_id, kind=kind, payload=payload or {})
        )

    async def get(self, id: UUID) -> IngestionJob | None:
        return await self._session.get(IngestionJob, id)

    async def list(
        self,
        *,
        status: str | None = None,
        source_id: UUID | None = None,
        limit: int | None = None,
    ) -> Sequence[IngestionJob]:
        stmt = apply_eq_filters(
            select(IngestionJob),
            {IngestionJob.status: status, IngestionJob.source_id: source_id},
        ).order_by(IngestionJob.enqueued_at.desc())
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

        Note: the row lock is released when the session commits, *not*
        when this method returns. The caller is expected to either
        commit immediately (and rely on the ``status = 'queued'`` filter
        below to keep the claim correct) or to hold the transaction
        open while processing. The UPDATE re-asserts the status filter
        as a defense against a duplicate-claim race introduced by a
        retry between SELECT and UPDATE.
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
        result = await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job.id, IngestionJob.status == "queued")
            .values(status="running", started_at=func.now(), attempts=IngestionJob.attempts + 1)
        )
        # rowcount is available on the underlying CursorResult; falling
        # through to refresh() would still work (we'd just get a row that
        # might already be `running`), so this is a defensive bail-out
        # for the rare lost-race case.
        if getattr(result, "rowcount", 1) == 0:
            return None
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
