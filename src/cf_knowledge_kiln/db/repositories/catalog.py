"""Catalog repositories: data sources and the model registry."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

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
