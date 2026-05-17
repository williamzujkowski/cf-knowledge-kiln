"""Eval-suite fixtures.

Reuses the integration suite's session-scoped fixtures via the
``pytest_plugins`` hook below so the eval tier inherits the same
migrations-applied DB and AsyncEngine without duplicating fixtures.

Seeds the repo's own ``docs/`` tree once per session under
:class:`MockEmbeddingProvider`. The vector arm is therefore degenerate
— bootstrap thresholds measure FTS + RRF stability only. A follow-up
issue should re-baseline under a real embedding provider before
promoting the gate to blocking.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource
from cf_knowledge_kiln.retrieval import HybridRetriever, load_retrieval_config

# Load the integration suite's session-scoped fixtures (database_url,
# engine, _apply_migrations). pytest_plugins must live in a top-level
# conftest, and tests/eval/conftest.py is one — sibling, not nested.
pytest_plugins = ["tests.integration.conftest"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_PATH = _REPO_ROOT / "tests" / "eval" / "golden" / "docs.yaml"
_DOCS_DIR = _REPO_ROOT / "docs"


def _eval_settings() -> Settings:
    return Settings(
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=500,
        ingest_max_repo_bytes=10 * 1_048_576,
    )


@pytest.fixture(scope="session")
def golden_path() -> Path:
    return _GOLDEN_PATH


@pytest.fixture
def seeded_db(database_url: str) -> Iterator[None]:
    """Ingest ``docs/`` per test under MockEmbeddingProvider.

    Function-scoped because the integration suite's autouse
    ``_truncate_between_tests`` wipes the DB before each test. We
    re-seed inside the same per-test transaction so the corpus is
    visible by the time the eval query runs. ~1-2 s per seed; cheap
    enough for the eval tier and avoids fighting the truncate.
    """

    async def _seed() -> None:
        eng: AsyncEngine = create_async_engine(database_url)
        try:
            maker = async_sessionmaker(eng, expire_on_commit=False)
            async with maker() as session:
                src = LocalSource(
                    name="cf-knowledge-kiln",
                    type="local",
                    path=str(_DOCS_DIR),
                    # Top-level docs only — ADR frontmatter currently
                    # contains YAML date objects that fail JSON
                    # serialization on the documents.metadata JSONB
                    # column. Tracked as #91; expand to ``**/*.md``
                    # once that fix lands.
                    include=["*.md"],
                )
                await run_source(
                    session,
                    source=src,
                    settings=_eval_settings(),
                    embedding_provider=MockEmbeddingProvider(),
                )
                await session.commit()
        finally:
            await eng.dispose()

    asyncio.run(_seed())
    yield


@pytest.fixture
def seeded_retriever(seeded_db: None, database_url: str) -> Iterator[HybridRetriever]:
    """Wire a :class:`HybridRetriever` against the seeded DB.

    Yields so the engine pool is disposed on teardown; a return-form
    fixture would leak one pool per test (the process tears down at
    suite end, but ``pytest -W error`` flags the open resource).
    """
    settings = _eval_settings()
    db = Database(database_url, pool_size=settings.pg_pool_size)
    config = load_retrieval_config(settings.security_config_path)
    retriever = HybridRetriever(
        db=db,
        embedding_provider=MockEmbeddingProvider(),
        config=config,
        ef_search=settings.hnsw_ef_search,
    )
    try:
        yield retriever
    finally:
        asyncio.run(db.dispose())
