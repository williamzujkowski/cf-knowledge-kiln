"""Add ``requester`` columns to ``rag_answers`` and ``context_packs`` (#315).

``rag_queries`` already carries a nullable ``requester`` column from
the initial schema; the API never populated it because there was no
identity surface yet. PR #315 lands OIDC SSO, which means every
authenticated browser request and every JWT-bearing agent request now
has a stable user identity — captured by the middleware and threaded
through ``request.state.username``.

This migration:

* Adds ``requester TEXT`` (nullable) to ``rag_answers`` so the
  /v1/answer telemetry row records who asked.
* Adds ``requester TEXT`` (nullable) to ``context_packs`` so the
  /v1/agent/context-pack telemetry row records which agent identity
  produced the pack.

Both columns are nullable because:

* Rows from before this migration exist with no requester.
* ``KILN_AUTH_MODE`` other than ``oidc`` doesn't surface an identity
  to capture, so the column stays NULL for those deployments.
* Agent integrations that bypass the middleware (test harnesses) must
  still be able to write telemetry rows.

A partial btree index on the non-NULL subset gives the "queries by
user X" audit query a cheap path without paying for an index slot on
the NULL majority. The same index is added to ``rag_queries`` for
symmetry (the column already exists there).

Revision ID: 0006_telemetry_requester
Revises: 0005_idempotency_keys
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_telemetry_requester"
down_revision: str | Sequence[str] | None = "0005_idempotency_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # rag_queries already has the column from 0001 — only the two
    # newer telemetry tables need it added.
    for table in ("rag_answers", "context_packs"):
        op.add_column(
            table,
            sa.Column("requester", sa.Text(), nullable=True),
        )
        op.create_index(
            f"ix_{table}_requester",
            table,
            ["requester"],
            postgresql_where=sa.text("requester IS NOT NULL"),
        )
    # rag_queries had no index on requester — add the same partial
    # index there for symmetry. Doesn't add the column because it
    # already exists from the initial schema.
    op.create_index(
        "ix_rag_queries_requester",
        "rag_queries",
        ["requester"],
        postgresql_where=sa.text("requester IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_rag_queries_requester", table_name="rag_queries")
    for table in ("rag_answers", "context_packs"):
        op.drop_index(f"ix_{table}_requester", table_name=table)
        op.drop_column(table, "requester")
