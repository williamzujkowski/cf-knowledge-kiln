"""rag_answers table for /v1/answer telemetry (#221).

PR #220 (Phase C of #192) wires the new ``POST /v1/answer`` route and
persists a minimal row into the shared ``rag_queries`` table tagged
``consumer_type='agent'``. That captures the query + retrieved chunks
but loses the answer-side signals an eval harness wants to slice on —
whether we synthesized vs refused, the refusal class, the generator
model + finish_reason, and the prompt/completion token counts.

This migration adds a dedicated ``rag_answers`` table that is a strict
superset of what ``rag_queries`` records for the answer endpoint. The
route is updated in the same PR to write here instead of (not
alongside) the shared queries table — duplicating into both would be
pure noise.

Indexes are scoped to the eval-time queries we expect:

* ``ix_rag_answers_created_at`` — time-series scans for distribution
  reports.
* ``ix_rag_answers_generator_model`` — per-model breakdowns
  (partial index — null on refusal-before-generation, so excluding
  nulls keeps the index small).

Revision ID: 0003_rag_answers
Revises: 0002_ingestion_jobs
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_rag_answers"
down_revision: str | Sequence[str] | None = "0002_ingestion_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rag_answers",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=True),
        sa.Column(
            "filters",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "evidence_chunk_ids",
            sa.dialects.postgresql.ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        # Response classification.
        sa.Column("answerable", sa.Boolean(), nullable=False),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=True),
        # Generator-side metadata — nullable for refusals that never
        # reached the generator (no-evidence + upstream-review paths).
        sa.Column("generator_provider", sa.Text(), nullable=True),
        sa.Column("generator_model", sa.Text(), nullable=True),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("requested_max_answer_tokens", sa.Integer(), nullable=False),
        # #202: clock_timestamp() not now() — see ingestion/pipeline.py.
        # Each row is a one-shot synthesis so we don't have a
        # started_at/finished_at pair; a single created_at is enough.
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence IN ('high', 'medium', 'low', 'none')",
            name="ck_rag_answers_confidence",
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_rag_answers_prompt_tokens_nonneg",
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_rag_answers_completion_tokens_nonneg",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_rag_answers_total_tokens_nonneg",
        ),
    )
    op.create_index(
        "ix_rag_answers_created_at",
        "rag_answers",
        ["created_at"],
        postgresql_ops={"created_at": "DESC"},
    )
    op.create_index(
        "ix_rag_answers_generator_model",
        "rag_answers",
        ["generator_model"],
        postgresql_where=sa.text("generator_model IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_rag_answers_generator_model", table_name="rag_answers")
    op.drop_index("ix_rag_answers_created_at", table_name="rag_answers")
    op.drop_table("rag_answers")
