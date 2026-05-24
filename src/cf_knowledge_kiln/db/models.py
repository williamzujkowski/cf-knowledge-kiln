"""SQLAlchemy 2.x ORM models for the 9 plan-defined tables.

Models mirror the hand-authored Alembic migration in
``alembic/versions/0001_initial_schema.py``. Alembic is **not**
auto-generated from this metadata — the migration is the source of
truth — but the schemas are kept in lockstep so repository code reads
typed rows.

The ``chunk_embeddings.embedding`` column is unconstrained ``vector``
so the table can hold rows from multiple embedding models
simultaneously. Retrieval queries filter on ``dimensions`` and cast as
needed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Date as SADate,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


def _pk() -> Mapped[UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class ModelRegistryEntry(Base):
    __tablename__ = "model_registry"
    __table_args__ = (
        UniqueConstraint("provider", "name", name="uq_model_registry_provider_name"),
        CheckConstraint("kind IN ('embedding', 'generator')", name="ck_model_registry_kind"),
    )

    id: Mapped[UUID] = _pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("repo", "path", name="uq_documents_repo_path"),)

    id: Mapped[UUID] = _pk()
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    system: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority: Mapped[str | None] = mapped_column(Text, nullable=True)
    sensitivity: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_reviewed: Mapped[date | None] = mapped_column(SADate, nullable=True)
    supersedes: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    superseded_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[UUID] = _pk()
    source_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    errors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunks_doc_index"),)

    id: Mapped[UUID] = _pk()
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class ChunkEmbedding(Base):
    __tablename__ = "chunk_embeddings"

    chunk_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts()


class RagQuery(Base):
    __tablename__ = "rag_queries"
    __table_args__ = (
        CheckConstraint("consumer_type IN ('human', 'agent')", name="ck_queries_consumer_type"),
    )

    id: Mapped[UUID] = _pk()
    query: Mapped[str] = mapped_column(Text, nullable=False)
    requester: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumer_type: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    retrieved_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = _ts()


class RagFeedback(Base):
    __tablename__ = "rag_feedback"

    id: Mapped[UUID] = _pk()
    query_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rag_queries.id", ondelete="SET NULL"),
        nullable=True,
    )
    chunk_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="SET NULL"),
        nullable=True,
    )
    signal: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class ContextPack(Base):
    __tablename__ = "context_packs"

    id: Mapped[UUID] = _pk()
    query: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    evidence_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = _ts()


class RagAnswer(Base):
    """#221: per-request telemetry for ``POST /v1/answer``.

    Strict superset of what ``rag_queries`` records for the answer
    endpoint. Captures the response classification (synthesized vs
    refused, refusal class via ``refusal_reason``), generator metadata
    (provider/model/finish_reason — null on refusals that never
    reached the generator), and honest token counts (null when the
    provider didn't return a ``usage`` block, never ``0``). The
    /v1/answer route writes here instead of (not alongside) the
    shared ``rag_queries`` table.
    """

    __tablename__ = "rag_answers"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR confidence IN ('high', 'medium', 'low', 'none')",
            name="ck_rag_answers_confidence",
        ),
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_rag_answers_prompt_tokens_nonneg",
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_rag_answers_completion_tokens_nonneg",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_rag_answers_total_tokens_nonneg",
        ),
    )

    id: Mapped[UUID] = _pk()
    query: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str | None] = mapped_column(Text, nullable=True)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    evidence_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    answerable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    generator_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    generator_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_max_answer_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # #202: use clock_timestamp() at write time, not now() (which
    # returns the transaction start). Each row is one-shot — no
    # started/finished pair — so a single created_at is enough.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('full_resync', 'incremental', 'single_doc')",
            name="ck_ingestion_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ingestion_jobs_status",
        ),
    )

    id: Mapped[UUID] = _pk()
    source_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="full_resync")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    enqueued_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    result_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
