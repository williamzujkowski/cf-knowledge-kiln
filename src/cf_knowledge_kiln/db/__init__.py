"""Database access layer (asyncpg + pgvector + SQLAlchemy 2 + Alembic).

Public surface:

* :func:`parse_vcap_services` / :func:`resolve_database_url` — config helpers.
* :class:`Database` — engine + session factory + readiness ping.
* Models in :mod:`.models`, repositories in :mod:`.repositories`.
"""

from cf_knowledge_kiln.db.connection import (
    Database,
    parse_vcap_services,
    redact_dsn,
    resolve_database_url,
)

__all__ = [
    "Database",
    "parse_vcap_services",
    "redact_dsn",
    "resolve_database_url",
]
