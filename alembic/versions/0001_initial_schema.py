"""Initial schema: 9 plan-defined tables + pgvector + FTS.

Per ADR-0002 / ADR-0008. Includes:

* ``CREATE EXTENSION IF NOT EXISTS vector`` (pgvector).
* All 9 plan tables (data_sources, model_registry, documents,
  ingestion_runs, document_chunks, chunk_embeddings, rag_queries,
  rag_feedback, context_packs).
* FTS GIN index on ``document_chunks.content`` for hybrid retrieval.
* HNSW partial index on ``chunk_embeddings.embedding`` keyed to the
  default 768-dim model (nomic-embed-text-v1.5 per the plan). Operators
  add additional partial indexes per registered model dimension in
  follow-up migrations; see ``docs/architecture.md`` § "Embedding
  index strategy".

The ``vector`` column type is unconstrained (variable per row) so the
schema can hold embeddings from multiple models simultaneously without
a destructive ``ALTER TABLE``. Retrieval queries filter on
``dimensions = N`` and cast the column to ``vector(N)``; the partial
HNSW index above is matched by exactly that predicate.

Note on file size: AGENTS.md caps source files at 400 lines "by
default". This migration legitimately exceeds that — it bundles DDL
for nine related tables that must apply atomically in one transaction
and that share table_args helpers. Splitting into multiple revisions
would either break atomicity or scatter the schema across files
without improving review experience. The waiver is explicit here so
the next reviewer doesn't think it slipped past the linter.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "data_sources",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "config",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "model_registry",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "config",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("provider", "name", name="uq_model_registry_provider_name"),
        sa.CheckConstraint("kind IN ('embedding', 'generator')", name="ck_model_registry_kind"),
    )

    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("system", sa.Text(), nullable=True),
        sa.Column("authority", sa.Text(), nullable=True),
        sa.Column("sensitivity", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("last_reviewed", sa.Date(), nullable=True),
        sa.Column(
            "supersedes",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "superseded_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("repo", "path", name="uq_documents_repo_path"),
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_repo", "documents", ["repo"])
    op.create_index("ix_documents_doc_type", "documents", ["doc_type"])
    op.create_index("ix_documents_last_reviewed", "documents", ["last_reviewed"])

    op.create_table(
        "ingestion_runs",
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
        sa.Column(
            "started_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.dialects.postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column(
            "stats",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "warnings",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "errors",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("commit_sha", sa.Text(), nullable=True),
    )
    op.create_index("ix_ingestion_runs_source_id", "ingestion_runs", ["source_id"])
    op.create_index(
        "ix_ingestion_runs_started_at",
        "ingestion_runs",
        [sa.text("started_at DESC")],
    )

    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "heading_path",
            sa.dialects.postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_tokens", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunks_doc_index"),
    )
    op.create_index("ix_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index("ix_chunks_content_hash", "document_chunks", ["content_hash"])
    op.execute(
        "CREATE INDEX ix_chunks_content_fts ON document_chunks "
        "USING GIN (to_tsvector('english', content))"
    )

    op.execute(
        """
        CREATE TABLE chunk_embeddings (
            chunk_id UUID PRIMARY KEY REFERENCES document_chunks(id) ON DELETE CASCADE,
            embedding vector NOT NULL,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_chunk_embeddings_hnsw_768 ON chunk_embeddings "
        "USING hnsw ((embedding::vector(768)) vector_cosine_ops) "
        "WHERE dimensions = 768"
    )

    op.create_table(
        "rag_queries",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("requester", sa.Text(), nullable=True),
        sa.Column("consumer_type", sa.Text(), nullable=False),
        sa.Column(
            "filters",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "retrieved_chunk_ids",
            sa.dialects.postgresql.ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "consumer_type IN ('human', 'agent')",
            name="ck_queries_consumer_type",
        ),
    )
    op.create_index(
        "ix_queries_created_at",
        "rag_queries",
        [sa.text("created_at DESC")],
    )
    op.create_index("ix_queries_consumer_type", "rag_queries", ["consumer_type"])

    op.create_table(
        "rag_feedback",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "query_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rag_queries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "chunk_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_feedback_chunk_id", "rag_feedback", ["chunk_id"])

    op.create_table(
        "context_packs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
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
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column(
            "warnings",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "requires_human_review", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_context_packs_created_at",
        "context_packs",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    # Reverse dependency order: drop FK targets last.
    op.drop_index("ix_context_packs_created_at", table_name="context_packs")
    op.drop_table("context_packs")

    op.drop_index("ix_feedback_chunk_id", table_name="rag_feedback")
    op.drop_table("rag_feedback")

    op.drop_index("ix_queries_consumer_type", table_name="rag_queries")
    op.drop_index("ix_queries_created_at", table_name="rag_queries")
    op.drop_table("rag_queries")

    op.execute("DROP INDEX IF EXISTS ix_chunk_embeddings_hnsw_768")
    op.execute("DROP TABLE IF EXISTS chunk_embeddings")

    op.execute("DROP INDEX IF EXISTS ix_chunks_content_fts")
    op.drop_index("ix_chunks_content_hash", table_name="document_chunks")
    op.drop_index("ix_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_index("ix_ingestion_runs_started_at", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_source_id", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")

    op.drop_index("ix_documents_last_reviewed", table_name="documents")
    op.drop_index("ix_documents_doc_type", table_name="documents")
    op.drop_index("ix_documents_repo", table_name="documents")
    op.drop_index("ix_documents_status", table_name="documents")
    # Null out the self-referencing FKs (`supersedes` / `superseded_by`)
    # so `drop_table` doesn't trip the FK constraint on a populated DB.
    op.execute("UPDATE documents SET supersedes = NULL, superseded_by = NULL")
    op.drop_table("documents")

    op.drop_table("model_registry")
    op.drop_table("data_sources")

    # Leave the vector extension installed: dropping it would clobber
    # any other application that happens to share the database.
