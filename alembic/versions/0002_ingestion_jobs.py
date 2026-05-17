"""ingestion_jobs queue table (#40).

A Postgres-backed job queue for the ingestion worker. Concurrent
workers use ``SELECT ... FOR UPDATE SKIP LOCKED`` to claim a job
without double-processing.

Columns mirror the issue scope: source_id (FK to data_sources), kind
(full_resync | incremental | single_doc), status (queued | running |
succeeded | failed | cancelled), attempts, last_error, payload JSON,
and result_run_id (FK to ingestion_runs).

Revision ID: 0002_ingestion_jobs
Revises: 0001_initial_schema
Create Date: 2026-05-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_ingestion_jobs"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_jobs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'full_resync'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column(
            "enqueued_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "started_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result_run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "kind IN ('full_resync', 'incremental', 'single_doc')",
            name="ck_ingestion_jobs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ingestion_jobs_status",
        ),
    )
    op.create_index("ix_jobs_status", "ingestion_jobs", ["status"])
    op.create_index(
        "ix_jobs_enqueued_at",
        "ingestion_jobs",
        ["enqueued_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_enqueued_at", table_name="ingestion_jobs")
    op.drop_index("ix_jobs_status", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
