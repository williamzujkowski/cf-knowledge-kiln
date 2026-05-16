"""Common base for thin async repositories."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Hold a single :class:`AsyncSession`.

    Sessions are not managed inside the repo. Callers are expected to
    use :meth:`cf_knowledge_kiln.db.Database.session` and to ``commit``
    / ``rollback`` themselves. This makes it natural to compose
    multiple repository operations in a single transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
