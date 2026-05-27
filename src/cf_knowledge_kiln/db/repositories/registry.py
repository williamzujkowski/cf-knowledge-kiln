"""Filter-vocabulary aggregator for ``GET /v1/registry`` (#359).

Aggregates per-dimension value lists + counts + most-recent
``last_reviewed`` dates from the ``documents`` table so an agent
can validate its filter values before sending. Without this an
agent has two silent-failure paths:

1. Send a filter value the kiln doesn't recognize → 400
   ``invalid_filter_value`` (loud).
2. Send a filter value the kiln DOES recognize but that has zero
   indexed documents → 200 with empty ``evidence`` (silent;
   indistinguishable from "no matching docs").

The registry surface lets the agent route around case 2.

The route is read-only and aggregates ``documents`` table state;
the registry-cache layer in :mod:`cf_knowledge_kiln.api.registry`
wraps this repository with a TTL so a high-QPS bootstrap callsite
doesn't re-aggregate per request.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import func, select

from cf_knowledge_kiln.db.models import Document
from cf_knowledge_kiln.db.repositories._base import BaseRepository

# Closed set of registry dimensions ↔ ORM column attributes. The
# column attribute is exposed so SQL aggregation hits the right
# column (avoids a getattr indirection per row). Order here is the
# order callers see in the response.
_DIMENSION_COLUMNS: dict[str, str] = {
    "status": "status",
    "doc_type": "doc_type",
    "owner": "owner",
    "repo": "repo",
    "authority": "authority",
    "sensitivity": "sensitivity",
    "system": "system",
}


class RegistryRow:
    """A single dimension-value aggregate row.

    Plain dataclass-ish container so the repo returns DB-shaped
    primitives; the route layer maps to :class:`RegistryValue`.
    """

    __slots__ = ("count", "last_indexed", "value")

    def __init__(self, *, value: str, count: int, last_indexed: date | None) -> None:
        self.value = value
        self.count = count
        self.last_indexed = last_indexed


class RegistryRepository(BaseRepository):
    """Aggregate filter vocabulary across :class:`Document` rows."""

    def supported_dimensions(self) -> list[str]:
        """Return the ordered list of dimension names the registry exposes."""
        return list(_DIMENSION_COLUMNS)

    async def aggregate(self, *, dimension: str) -> list[RegistryRow]:
        """Return value/count/last_indexed rows for a single dimension.

        NULL values are dropped — a filter passing NULL means "no
        constraint", not "match documents with no doc_type". Empty
        strings would be a poisoned value and are also dropped.

        The list is sorted by ``count`` desc so the most-populated
        bucket leads (most-useful filter value first). Ties break
        alphabetically by value for determinism.
        """
        column_name = _DIMENSION_COLUMNS.get(dimension)
        if column_name is None:
            raise ValueError(f"Unknown registry dimension: {dimension!r}")
        col = getattr(Document, column_name)
        stmt = (
            select(
                col.label("value"),
                # Label as ``n`` so the row attribute doesn't collide
                # with the ``count`` callable on the SQLAlchemy Row
                # type (mypy can't disambiguate the two).
                func.count().label("n"),
                func.max(Document.last_reviewed).label("last_indexed"),
            )
            .where(col.is_not(None))
            .where(col != "")
            .group_by(col)
            .order_by(func.count().desc(), col.asc())
        )
        result = await self._session.execute(stmt)
        return [
            RegistryRow(
                value=row.value,
                count=int(row.n),
                last_indexed=row.last_indexed,
            )
            for row in result.all()
        ]

    async def aggregate_all(self) -> dict[str, list[RegistryRow]]:
        """Aggregate every supported dimension; one query per dimension.

        Trade-off: a single multi-dimension UNION ALL query would be
        faster, but the schema makes it awkward — each dimension is
        a different column. The N=7 dimensions over a small Document
        table makes the per-dimension cost negligible, and the
        registry-cache layer in :mod:`cf_knowledge_kiln.api.registry`
        amortizes this anyway.
        """
        out: dict[str, list[RegistryRow]] = defaultdict(list)
        for dimension in _DIMENSION_COLUMNS:
            out[dimension] = await self.aggregate(dimension=dimension)
        return dict(out)
