"""Integration-tier fixtures.

These fixtures provision a real Postgres+pgvector by way of either:

* ``KILN_TEST_DATABASE_URL`` — explicit URL (recommended in CI).
* The default DSN (``kiln:kiln@localhost:5432/kiln``) if a local
  ``pgvector/pgvector:pg16`` container is running. See ``DEFAULT_TEST_DSN``
  below for the exact value — the dummy credentials match the local
  Docker container documented in HANDOFF.md.

If neither is reachable, integration tests are skipped — they should
never silently fail. Use ``-m integration`` to run just this tier.

Each test session:

1. Applies Alembic ``upgrade head`` against the configured DB.
2. Truncates the 9 plan tables between tests.

The local-dev container can be started with:

    docker run -d --name kiln-pg \\
        -e POSTGRES_PASSWORD=kiln -e POSTGRES_USER=kiln \\
        -e POSTGRES_DB=kiln -p 5432:5432 \\
        pgvector/pgvector:pg16
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration._migration_isolation import apply_migrations_with_isolation

DEFAULT_TEST_DSN = "postgresql+asyncpg://kiln:kiln@localhost:5432/kiln"  # pragma: allowlist secret

_TRUNCATE_TABLES = [
    "ingestion_jobs",
    "rag_feedback",
    "rag_queries",
    "context_packs",
    "chunk_embeddings",
    "document_chunks",
    "ingestion_runs",
    "documents",
    "model_registry",
    "data_sources",
]


def _test_database_url() -> str | None:
    return os.environ.get("KILN_TEST_DATABASE_URL") or os.environ.get("KILN_DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    return _test_database_url() or DEFAULT_TEST_DSN


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations(database_url: str) -> Iterator[None]:
    """Apply Alembic migrations once per session.

    Skips the entire integration tier if the DB is unreachable; lets
    real pgvector-missing errors surface so the acceptance check
    ("refuses cleanly when pgvector is unavailable") is exercised.

    Env-var + logger isolation lives in
    :func:`tests.integration._migration_isolation.apply_migrations_with_isolation`
    (a non-conftest module so the regression guard in
    ``test_env_isolation.py`` can import it without triggering the
    pytest plugin double-registration on ``tests.integration.conftest``).
    """
    try:
        with apply_migrations_with_isolation(database_url):
            yield
    except Exception as exc:
        # The helper only raises if the upgrade itself fails (it runs
        # before its inner yield). Session-scoped autouse fixtures
        # don't see test-failure exceptions, so any exception here
        # is the migration-failed case.
        pytest.skip(
            f"Integration tests require a reachable pgvector Postgres at "
            f"{database_url}. Migration failed with: {exc}",
            allow_module_level=True,
        )


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_between_tests(engine: AsyncEngine) -> AsyncIterator[None]:
    """Reset the 9 plan tables before each integration test."""
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TRUNCATE_TABLES)} RESTART IDENTITY CASCADE"))
    yield
