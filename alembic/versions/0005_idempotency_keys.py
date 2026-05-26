"""Add the idempotency_keys table (#309).

Issue #309: agent retries on a network blip would create
duplicate telemetry rows (same query+task+filters, two different
context_pack_ids). This migration backs the Idempotency-Key
contract that closes that hazard.

The table is per-route — same key against /v1/answer and
/v1/agent/context-pack are independent — so the PK is the
``(key, route)`` composite. Storing the full ``response_body`` +
``response_status`` lets the handler re-serve a byte-identical
response on replay, matching Stripe's documented semantics.

Indexed for the sweeper (``expires_at IS NOT NULL`` is always
true so a plain btree on the column wins over a partial).

Revision ID: 0005_idempotency_keys
Revises: 0004_request_id_columns
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005_idempotency_keys"
down_revision: str | Sequence[str] | None = "0004_request_id_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        # Composite PK — same key against two different routes is
        # NOT a collision. An agent that uses one key per logical
        # operation across multiple endpoints just works.
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        # SHA-256 of the canonicalized request body. The handler
        # compares this on a key-collision to detect agents
        # retrying with a different body (→ 422
        # idempotency_conflict per the design plan).
        sa.Column("request_hash", sa.Text(), nullable=False),
        # The wire-visible resource id (context_pack_id /
        # answer_id) or NULL when the original response was an
        # error envelope with no resource. Nullable because we
        # cache 4xx responses too.
        sa.Column("resource_id", sa.Text(), nullable=True),
        # Full response payload — JSONB so PostgreSQL stores it
        # compactly and we don't pay a TEXT decode + JSON parse
        # on replay. Carries the original request_id, so a replay
        # surfaces the ORIGINAL correlation key in the body even
        # though the replay's response header X-Request-ID
        # reflects the replay attempt's id. The
        # Idempotency-Replayed: true header is the signal that
        # the two differ on purpose.
        sa.Column("response_body", JSONB(), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # ``created_at + 24h`` set at insert time. Stored as a
        # column (not computed) so the sweeper's index hit is a
        # simple range query instead of a CASE expression.
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", "route", name="pk_idempotency_keys"),
    )
    # Sweeper index: ``DELETE WHERE expires_at < now()`` is the
    # access pattern; a plain btree on expires_at wins because
    # every row is in scope (expires_at is NOT NULL).
    op.create_index(
        "ix_idempotency_keys_expires_at",
        "idempotency_keys",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
