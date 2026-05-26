"""Add request_id columns to telemetry tables (#260 second half).

PR #265 (#260 first half) shipped the X-Request-ID middleware: every
request gets a stable correlation key, exposed on ``request.state``,
echoed in the response header, and stamped on the per-request log
line. This migration closes the loop by persisting that key on the
three telemetry tables an audit traces through:

* ``rag_queries`` — the per-request retrieval row
* ``rag_answers`` — the per-request /v1/answer row
* ``context_packs`` — the per-request /v1/agent/context-pack row

After this lands, an operator handed a request_id from a user
complaint can SELECT the telemetry row, see which chunks were
returned, and reconstruct what the agent saw — closing the audit
loop that #256 (wire-id persistence) opened.

The column is ``str | None`` because:

* Rows from before this migration exist with NULL.
* The middleware is opt-out via uninstall; a future test harness
  that constructs a bare app could write rows without one.

Indexed for the common "find by request_id" lookup.

Revision ID: 0004_request_id_columns
Revises: 0003_rag_answers
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_request_id_columns"
down_revision: str | Sequence[str] | None = "0003_rag_answers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Same length cap as the middleware's _MAX_LEN (200 chars after
# sanitization). Storing as Text keeps the DDL simple and removes a
# trap if the middleware's cap ever moves; the index works either way.
_REQUEST_ID = sa.Text()


def upgrade() -> None:
    # add_column inside a single op so an aborted run leaves a
    # consistent state (alembic wraps each migration in a transaction
    # by default).
    for table in ("rag_queries", "rag_answers", "context_packs"):
        op.add_column(
            table,
            sa.Column("request_id", _REQUEST_ID, nullable=True),
        )
        # Partial index on the non-null subset — pre-migration rows
        # have NULL and aren't worth indexing.
        op.create_index(
            f"ix_{table}_request_id",
            table,
            ["request_id"],
            postgresql_where=sa.text("request_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in ("rag_queries", "rag_answers", "context_packs"):
        op.drop_index(f"ix_{table}_request_id", table_name=table)
        op.drop_column(table, "request_id")
