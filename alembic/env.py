"""Alembic environment.

Resolves the DB URL via :func:`cf_knowledge_kiln.db.resolve_database_url`
so the same precedence rules apply in dev, CI, and Cloud Foundry.

We use SQLAlchemy's async engine and run migrations through
``connection.run_sync(...)``, which is the canonical pattern for async
Alembic in SQLAlchemy 2.x.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from cf_knowledge_kiln.config import get_settings
from cf_knowledge_kiln.db import resolve_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Phase 2's initial migration uses raw SQL (CREATE EXTENSION, pgvector
# index expressions, FTS GIN) that doesn't round-trip through a
# declarative metadata model. target_metadata stays None; future
# migrations can introduce ORM models without changing this pattern.
target_metadata = None


def _database_url() -> str:
    settings = get_settings()
    url = resolve_database_url(settings)
    if url is None:
        raise RuntimeError(
            "No database URL available. Set KILN_DATABASE_URL or bind a "
            "Postgres service so VCAP_SERVICES is populated."
        )
    return url


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection.

    Useful for dry-run review and for environments that apply migrations
    via a separately-piped SQL script.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_async() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Apply migrations against the live async engine."""
    asyncio.run(_run_migrations_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
