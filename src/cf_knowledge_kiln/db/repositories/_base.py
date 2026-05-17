"""Common base for thin async repositories.

Two shared helpers live here, both extracted in #49 from boilerplate
that was repeated across all 10 repositories:

* :meth:`BaseRepository._persist` — ``add → flush → refresh → return``,
  the "create one row and hand it back populated" idiom.
* :func:`apply_eq_filters` — translate a ``{Column: value | None}`` map
  into a chain of ``WHERE col = value`` predicates, skipping ``None``
  entries. The optional-filter ``if x is not None: stmt = stmt.where(...)``
  ladder collapses to one call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

T = TypeVar("T")


class BaseRepository:
    """Hold a single :class:`AsyncSession`.

    Sessions are not managed inside the repo. Callers are expected to
    use :meth:`cf_knowledge_kiln.db.Database.session` and to ``commit``
    / ``rollback`` themselves. This makes it natural to compose
    multiple repository operations in a single transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _persist(self, row: T) -> T:
        """``session.add → flush → refresh → return row``.

        The common Pattern A from #49: every ``create()`` method ends
        with this exact four-line sequence. The flush populates
        server-side defaults (UUIDs, timestamps) and the refresh loads
        them back into the ORM object so the caller sees a fully-
        populated row.
        """
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row


def apply_eq_filters(stmt: Select[Any], filters: Mapping[Any, Any]) -> Select[Any]:
    """Add ``WHERE col = value`` for every non-``None`` entry in ``filters``.

    Lets a repository's ``list()`` method express its filter ladder as
    a single dict instead of an `if x is not None: stmt = stmt.where()`
    repetition for every column. ``None`` is "no constraint".

    Example::

        stmt = apply_eq_filters(
            select(Document),
            {Document.status: status, Document.repo: repo},
        )
    """
    for col, value in filters.items():
        if value is not None:
            stmt = stmt.where(col == value)
    return stmt


__all__ = ["BaseRepository", "apply_eq_filters"]
