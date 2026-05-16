"""Catalog repositories: data sources and the model registry."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from cf_knowledge_kiln.db.models import DataSource, ModelRegistryEntry
from cf_knowledge_kiln.db.repositories._base import BaseRepository


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
        row = DataSource(
            name=name, type=type, location=location, status=status, config=config or {}
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> DataSource | None:
        return await self._session.get(DataSource, id)

    async def list(
        self, *, status: str | None = None, type: str | None = None
    ) -> Sequence[DataSource]:
        stmt = select(DataSource)
        if status is not None:
            stmt = stmt.where(DataSource.status == status)
        if type is not None:
            stmt = stmt.where(DataSource.type == type)
        stmt = stmt.order_by(DataSource.created_at.desc())
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
        row = ModelRegistryEntry(
            kind=kind,
            provider=provider,
            name=name,
            dimensions=dimensions,
            base_url=base_url,
            enabled=enabled,
            config=config or {},
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, id: UUID) -> ModelRegistryEntry | None:
        return await self._session.get(ModelRegistryEntry, id)

    async def list(
        self,
        *,
        kind: str | None = None,
        provider: str | None = None,
        enabled: bool | None = None,
    ) -> Sequence[ModelRegistryEntry]:
        stmt = select(ModelRegistryEntry)
        if kind is not None:
            stmt = stmt.where(ModelRegistryEntry.kind == kind)
        if provider is not None:
            stmt = stmt.where(ModelRegistryEntry.provider == provider)
        if enabled is not None:
            stmt = stmt.where(ModelRegistryEntry.enabled.is_(enabled))
        stmt = stmt.order_by(ModelRegistryEntry.created_at.desc())
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, id: UUID) -> None:
        await self._session.execute(delete(ModelRegistryEntry).where(ModelRegistryEntry.id == id))
