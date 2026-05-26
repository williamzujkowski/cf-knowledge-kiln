"""Repository for the idempotency_keys table (#309).

Per-route replay cache. The handler-side dispatch in
:mod:`cf_knowledge_kiln.api.idempotency` is the only caller —
keeps the repo + handler concerns separate so a future change
to the cache table shape doesn't ripple through the
``ContextPacksRepository`` / ``AnswersRepository`` modules
that already exist.

Three methods cover the contract:

* :meth:`lookup` — single PK fetch for the
  miss/hit/conflict decision in the dispatcher.
* :meth:`create` — insert one row on the miss branch (after
  the handler has produced a response to cache).
* :meth:`delete_expired` — sweeper; called from a follow-up
  CLI subcommand or background task. Not invoked on the
  request path so any failure is non-fatal.

Storage trade-off rationale lives in the PR description for
the foundation (#311) and the issue body of #309 — short
version: separate table beats per-table unique constraints
because it supports cross-route key reuse + 4xx caching +
single body-hash column.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from cf_knowledge_kiln.db.models import IdempotencyKey
from cf_knowledge_kiln.db.repositories._base import BaseRepository

# 24 h matches Stripe's window; long enough for an agent's retry
# budget + a human-in-the-loop pause, short enough that the table
# stays small. Single source of truth so the dispatcher and the
# repo can't drift on what "expired" means.
DEFAULT_TTL = timedelta(hours=24)


class IdempotencyRepository(BaseRepository):
    """CRUD for the idempotency replay cache."""

    async def lookup(self, *, key: str, route: str) -> IdempotencyKey | None:
        """Return the cached row for ``(key, route)`` or None.

        The dispatcher uses this to decide miss vs hit vs
        conflict. Expired rows are NOT auto-filtered here —
        the dispatcher checks ``expires_at`` so it can return
        miss + delete-as-side-effect rather than reading a row
        the sweeper just hasn't reached yet.
        """
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.key == key,
            IdempotencyKey.route == route,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        key: str,
        route: str,
        request_hash: str,
        resource_id: str | None,
        response_body: dict[str, Any],
        response_status: int,
        ttl: timedelta = DEFAULT_TTL,
    ) -> IdempotencyKey:
        """Insert one ``idempotency_keys`` row.

        ``ttl`` is a kwarg for testability; production callers
        pass the default 24h. ``expires_at`` is computed at
        insert time as ``now() + ttl`` so the sweeper's index
        does a plain btree range query instead of a CASE
        expression on every row.
        """
        return await self._persist(
            IdempotencyKey(
                key=key,
                route=route,
                request_hash=request_hash,
                resource_id=resource_id,
                response_body=response_body,
                response_status=response_status,
                expires_at=datetime.now(UTC) + ttl,
            )
        )

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        """Drop expired rows; return the number deleted.

        ``now`` is a kwarg so tests can pin the cutoff
        deterministically. Production callers omit it (defaults
        to UTC now()). The sweeper isn't on the request path —
        a CLI subcommand or background task invokes it.
        """
        cutoff = now or datetime.now(UTC)
        result = await self._session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at < cutoff)
        )
        # rowcount is 0 when nothing matched (no exception); the
        # sweeper's caller can log the count for visibility.
        return getattr(result, "rowcount", 0) or 0

    async def list(
        self,
        *,
        route: str | None = None,
        limit: int | None = None,
    ) -> Sequence[IdempotencyKey]:
        """List cached rows. Operational tool only — not used by
        the dispatcher. Useful for audits / debugging."""
        stmt = select(IdempotencyKey).order_by(IdempotencyKey.created_at.desc())
        if route is not None:
            stmt = stmt.where(IdempotencyKey.route == route)
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.execute(stmt)).scalars().all()
