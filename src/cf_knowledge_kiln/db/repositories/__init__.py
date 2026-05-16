"""Thin async repositories per plan-defined entity.

Each repository wraps a single :class:`AsyncSession` and exposes four
methods: ``create``, ``get``, ``list``, ``delete``. Business logic does
not live here — that belongs in the retrieval, ingestion, and agent
modules. Repositories serialize knowledge of how each entity is
persisted, nothing more.
"""

from cf_knowledge_kiln.db.repositories.catalog import (
    DataSourcesRepository,
    ModelRegistryRepository,
)
from cf_knowledge_kiln.db.repositories.documents import (
    ChunksRepository,
    DocumentsRepository,
    EmbeddingsRepository,
)
from cf_knowledge_kiln.db.repositories.operations import (
    ContextPacksRepository,
    FeedbackRepository,
    IngestionRunsRepository,
    QueriesRepository,
)

__all__ = [
    "ChunksRepository",
    "ContextPacksRepository",
    "DataSourcesRepository",
    "DocumentsRepository",
    "EmbeddingsRepository",
    "FeedbackRepository",
    "IngestionRunsRepository",
    "ModelRegistryRepository",
    "QueriesRepository",
]
