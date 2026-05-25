"""Catalog repositories: data sources and the model registry."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cf_knowledge_kiln.db.models import DataSource, ModelRegistryEntry
from cf_knowledge_kiln.db.repositories._base import BaseRepository, apply_eq_filters


class DataSourcesRepository(BaseRepository):
    async def create(
        self,
        *,
        name: str,
        type: str,
        location: str,
        status: str = "active",
        config: dict[str, Any] | None = None,
    ) -> DataSource:
        return await self._persist(
            DataSource(name=name, type=type, location=location, status=status, config=config or {})
        )

    async def get_or_create(
        self,
        *,
        name: str,
        type: str,
        location: str,
        status: str = "active",
        config: dict[str, Any] | None = None,
    ) -> DataSource:
        """Idempotent INSERT-or-fetch for the row keyed by ``name``.

        Uses ``INSERT ... ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING *`` so we always get a populated row regardless of
        whether the INSERT actually inserted. The no-op SET on the
        conflict path is what makes RETURNING fire — ``DO NOTHING``
        does NOT return a row and would require a second SELECT.

        Solves #239: the previous ``list()`` → ``create()`` pattern
        opened a long-held row-level lock on ``data_sources`` (held
        until the surrounding session committed, which only happened
        AFTER the entire embed phase — minutes later). A concurrent
        worker upserting the same source name would block for the
        full duration. The UPSERT here is one statement; the lock is
        released as soon as the caller commits, which the pipeline
        now does immediately after the catalog setup.
        """
        stmt = (
            pg_insert(DataSource)
            .values(name=name, type=type, location=location, status=status, config=config or {})
            .on_conflict_do_update(
                index_elements=["name"],
                # No-op SET — required for RETURNING to fire on conflict.
                # The existing row's other fields are NOT overwritten; if
                # operators want type/location/status drift to update on
                # re-ingest, that's a separate policy choice (today we
                # leave existing-row fields untouched).
                set_={"name": pg_insert(DataSource).excluded.name},
            )
            .returning(DataSource)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def get(self, id: UUID) -> DataSource | None:
        return await self._session.get(DataSource, id)

    async def list(
        self, *, status: str | None = None, type: str | None = None
    ) -> Sequence[DataSource]:
        stmt = apply_eq_filters(
            select(DataSource),
            {DataSource.status: status, DataSource.type: type},
        ).order_by(DataSource.created_at.desc())
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(DataSource).where(DataSource.id == id))


class ModelRegistryRepository(BaseRepository):
    async def create(
        self,
        *,
        kind: str,
        provider: str,
        name: str,
        dimensions: int | None = None,
        base_url: str | None = None,
        enabled: bool = False,
        config: dict[str, Any] | None = None,
    ) -> ModelRegistryEntry:
        return await self._persist(
            ModelRegistryEntry(
                kind=kind,
                provider=provider,
                name=name,
                dimensions=dimensions,
                base_url=base_url,
                enabled=enabled,
                config=config or {},
            )
        )

    async def get(self, id: UUID) -> ModelRegistryEntry | None:
        return await self._session.get(ModelRegistryEntry, id)

    async def list(
        self,
        *,
        kind: str | None = None,
        provider: str | None = None,
        enabled: bool | None = None,
    ) -> Sequence[ModelRegistryEntry]:
        stmt = apply_eq_filters(
            select(ModelRegistryEntry),
            {
                ModelRegistryEntry.kind: kind,
                ModelRegistryEntry.provider: provider,
                ModelRegistryEntry.enabled: enabled,
            },
        ).order_by(ModelRegistryEntry.created_at.desc())
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(ModelRegistryEntry).where(ModelRegistryEntry.id == id))
