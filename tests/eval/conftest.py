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
from typing import Any

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
_ADVERSARIAL_DIR = _REPO_ROOT / "tests" / "eval" / "fixtures" / "adversarial"

# Phrases the prompt-injection scanner watches for. Kept in sync with
# the production list at config/security.example.yaml; the journey
# tests need the scanner enabled so the prompt-injection adversarial
# fixture actually gets stamped at ingest time.
_PROMPT_INJECTION_PHRASES = [
    "ignore previous instructions",
    "ignore prior instructions",
    "disregard the system prompt",
    "you are now",
    "developer message",
    "you must comply",
]


def _sensitive_patterns() -> list[Any]:
    """Regex patterns the sensitive-content scanner watches for (#100).

    Kept in sync with config/security.example.yaml. Built lazily so the
    import-order doesn't drag re/scanner internals into the module
    top-level — the journey tests are the only callers.
    """
    import re

    from cf_knowledge_kiln.ingestion.sensitive_content import _CompiledPattern

    raw = [
        r"AKIA[0-9A-Z]{16}",
        r"xox[baprs]-[0-9a-zA-Z]{10,}",
        r"gh[ps]_[0-9a-zA-Z]{36,}",
        # Eval-only synthetic marker — avoids gitleaks false-positives
        # on the adversarial fixture while still proving the scanner
        # wire-up works end-to-end.
        r"INTERNAL-SECRET-[A-Z0-9]{8}",
    ]
    return [_CompiledPattern(source=s, compiled=re.compile(s)) for s in raw]


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
                    include=["**/*.md"],
                    # #108: docs/_eval/ is the review-precision corpus
                    # (conflict pair, sensitive marker, prompt-injection
                    # trap) — its own conftest seeds it; the journey +
                    # golden tiers must NOT see it or their queries
                    # against the real docs/ tree get polluted.
                    exclude=["_eval/**"],
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
def seeded_db_with_adversarial(database_url: str) -> Iterator[None]:
    """Seed docs/ AND tests/eval/fixtures/adversarial/ with PI scanning on.

    Used by the journey-level tests (#68): the adversarial fixtures
    are what exercise the prompt-injection warning emission and the
    deprecation-status flow. Function-scoped for the same reason as
    ``seeded_db`` — autouse truncate races a session-scoped seed.
    """

    async def _seed() -> None:
        eng: AsyncEngine = create_async_engine(database_url)
        try:
            maker = async_sessionmaker(eng, expire_on_commit=False)
            async with maker() as session:
                # The repo docs/ stay scan-clean — the production
                # filter list never matches benign architecture or
                # security prose.
                sensitive = _sensitive_patterns()
                await run_source(
                    session,
                    source=LocalSource(
                        name="cf-knowledge-kiln",
                        type="local",
                        path=str(_DOCS_DIR),
                        include=["**/*.md"],
                        exclude=["_eval/**"],  # see seeded_db note above (#108)
                    ),
                    settings=_eval_settings(),
                    embedding_provider=MockEmbeddingProvider(),
                    prompt_injection_phrases=_PROMPT_INJECTION_PHRASES,
                    sensitive_patterns=sensitive,
                )
                await run_source(
                    session,
                    source=LocalSource(
                        name="adversarial-fixtures",
                        type="local",
                        path=str(_ADVERSARIAL_DIR),
                        include=["*.md"],
                    ),
                    settings=_eval_settings(),
                    embedding_provider=MockEmbeddingProvider(),
                    prompt_injection_phrases=_PROMPT_INJECTION_PHRASES,
                    sensitive_patterns=sensitive,
                )
                await session.commit()
        finally:
            await eng.dispose()

    asyncio.run(_seed())
    yield


def _build_retriever(database_url: str) -> tuple[HybridRetriever, Database]:
    settings = _eval_settings()
    db = Database(database_url, pool_size=settings.pg_pool_size)
    config = load_retrieval_config(settings.security_config_path)
    # #161: under MockEmbeddingProvider the RRF fused score collapses
    # near zero — far below the production 0.015 relevance floor. The
    # journey suite's adversarial tests need the per-chunk warnings to
    # fire so we relax the floor for the mock path. The rank gate
    # (max_warning_rank=3 default) still applies, which is what the
    # journey suite is actually exercising. Under real embeddings the
    # floor reads from config and stays strict.
    config = config.model_copy(update={"relevance_floor": 1e-4})
    retriever = HybridRetriever(
        db=db,
        embedding_provider=MockEmbeddingProvider(),
        config=config,
        ef_search=settings.hnsw_ef_search,
    )
    return retriever, db


@pytest.fixture
def seeded_retriever(seeded_db: None, database_url: str) -> Iterator[HybridRetriever]:
    """Wire a :class:`HybridRetriever` against the seeded DB.

    Yields so the engine pool is disposed on teardown; a return-form
    fixture would leak one pool per test (the process tears down at
    suite end, but ``pytest -W error`` flags the open resource).
    """
    retriever, db = _build_retriever(database_url)
    try:
        yield retriever
    finally:
        asyncio.run(db.dispose())


@pytest.fixture
def adversarial_retriever(
    seeded_db_with_adversarial: None, database_url: str
) -> Iterator[HybridRetriever]:
    """Same as :func:`seeded_retriever` but with adversarial fixtures ingested."""
    retriever, db = _build_retriever(database_url)
    try:
        yield retriever
    finally:
        asyncio.run(db.dispose())
