"""Integration tests for /v1/search and /v1/agent/context-pack (Phase 5 slice 4).

End-to-end through real FastAPI handlers backed by a live pgvector DB:
seed corpus → POST → 200 response → assert response shape + persistence
to rag_queries / context_packs tables.

These tests own the FastAPI lifespan (TestClient context manager) so
the DB pool + embedding provider are wired up exactly as in production.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db.models import ContextPack, RagQuery
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    return Settings(
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=100,
        ingest_max_repo_bytes=10 * 1_048_576,
    )


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def small_corpus(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text(
        textwrap.dedent(
            """\
            # Alpha
            zebra alpha unique-token-aaa.
            """
        )
    )
    (tmp_path / "beta.md").write_text(
        textwrap.dedent(
            """\
            # Beta
            beta documentation about widgets and gadgets.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus_dir: Path) -> None:
    src = LocalSource(name="api-tests", type="local", path=str(corpus_dir), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    """A TestClient bound to a fresh app with the live DB URL exported.

    Uses ``with`` so the FastAPI lifespan starts the DB pool +
    embedding provider just like in production.
    """
    saved = os.environ.get("KILN_DATABASE_URL")
    os.environ["KILN_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        if saved is None:
            os.environ.pop("KILN_DATABASE_URL", None)
        else:
            os.environ["KILN_DATABASE_URL"] = saved
        get_settings.cache_clear()


# ─── /v1/search ──────────────────────────────────────────────────────


def test_search_returns_200_with_results(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """Smoke: POST → 200 → SearchResponse with at least one result."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    response = client.post("/v1/search", json={"query": "widgets", "max_results": 5})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == "widgets"
    assert body["results"]
    top = body["results"][0]
    assert top["chunk_id"]
    assert top["document_id"]
    # Real title, not the "(unknown)" fallback. Proves document_refs
    # are actually plumbed through, not silently degraded.
    assert top["title"] and top["title"] != "(unknown)"
    # Real excerpt from chunk content (was always-empty before slice 4
    # fix that plumbed chunk_text through SearchResult).
    assert top["excerpt"], "excerpt must be the chunk content, not empty"
    assert top["status"]
    assert top["score"] >= 0


def test_search_400_on_empty_query(client: TestClient) -> None:
    """Pydantic min_length=1 on query → FastAPI returns 422."""
    response = client.post("/v1/search", json={"query": ""})
    assert response.status_code == 422


def test_search_422_on_missing_query(client: TestClient) -> None:
    response = client.post("/v1/search", json={})
    assert response.status_code == 422


def test_search_persists_rag_query_row(
    client: TestClient, session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """Each /v1/search request appends a rag_queries row."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    response = client.post("/v1/search", json={"query": "widgets"})
    assert response.status_code == 200

    # Verify the rag_queries row landed.
    async def _count() -> int:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            rows = (await s.execute(select(RagQuery))).scalars().all()
            return len(rows)

    n = asyncio.get_event_loop().run_until_complete(_count())
    assert n == 1


def test_search_returns_200_when_telemetry_write_fails(
    client: TestClient,
    session: AsyncSession,
    small_corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#172: a telemetry-write failure must NOT cascade to a 500.

    `_log_rag_query` wraps the `rag_queries` insert in a `begin_nested()`
    savepoint and swallows failures (HANDOFF trap #21). Simulate the
    insert raising and confirm the search response still succeeds.
    """
    import asyncio

    from cf_knowledge_kiln.db.repositories import QueriesRepository

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    async def _boom(self: object, **kwargs: object) -> object:
        raise RuntimeError("simulated telemetry DB failure")

    monkeypatch.setattr(QueriesRepository, "create", _boom)

    response = client.post("/v1/search", json={"query": "widgets"})
    assert response.status_code == 200
    assert "results" in response.json()


# ─── /v1/agent/context-pack ─────────────────────────────────────────


def test_context_pack_returns_200_with_evidence(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    response = client.post(
        "/v1/agent/context-pack",
        json={
            "task": "summarize widget docs",
            "query": "widgets",
            "max_chunks": 5,
            "max_tokens": 2000,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["context_pack_id"]
    assert body["token_budget"]["requested"] == 2000
    assert body["evidence"], "expected at least one evidence chunk"
    assert body["untrusted_content_notice"]


def test_context_pack_422_on_missing_task(client: TestClient) -> None:
    response = client.post("/v1/agent/context-pack", json={"query": "x"})
    assert response.status_code == 422


def test_context_pack_persists_context_packs_row(
    client: TestClient, session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    """Each /v1/agent/context-pack request appends a context_packs row."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    response = client.post(
        "/v1/agent/context-pack",
        json={"task": "explain", "query": "widgets", "max_chunks": 3, "max_tokens": 500},
    )
    assert response.status_code == 200

    async def _count() -> int:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            rows = (await s.execute(select(ContextPack))).scalars().all()
            return len(rows)

    n = asyncio.get_event_loop().run_until_complete(_count())
    assert n == 1


# ─── #74: single DB session per request ─────────────────────────────


def test_search_request_uses_one_db_session(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """Issue #74: a /v1/search request must check out exactly ONE session.

    Before #74 the handler opened one session inside the engine for
    the CTE and a second inside the telemetry writer — capping
    concurrency at ~8 with default pool settings. After #74 the
    handler opens one session via get_session and passes it to both
    paths.

    We assert this by patching Database.session to count calls during
    a single request.
    """
    import asyncio
    from unittest.mock import patch

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    from cf_knowledge_kiln.db.connection import Database

    real_session = Database.session
    calls: list[int] = []

    def counting_session(self: Database) -> Any:
        calls.append(1)
        return real_session(self)

    with patch.object(Database, "session", counting_session):
        response = client.post("/v1/search", json={"query": "widgets"})
    assert response.status_code == 200
    assert sum(calls) == 1, (
        f"expected ONE Database.session() call per /v1/search request, "
        f"got {sum(calls)}. #74 regression — telemetry should share the "
        f"handler's session, not open its own."
    )


def test_context_pack_request_uses_one_db_session(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """Issue #74: a /v1/agent/context-pack request must check out exactly ONE session."""
    import asyncio
    from unittest.mock import patch

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    from cf_knowledge_kiln.db.connection import Database

    real_session = Database.session
    calls: list[int] = []

    def counting_session(self: Database) -> Any:
        calls.append(1)
        return real_session(self)

    with patch.object(Database, "session", counting_session):
        response = client.post(
            "/v1/agent/context-pack",
            json={"task": "explain", "query": "widgets"},
        )
    assert response.status_code == 200
    assert sum(calls) == 1, (
        f"expected ONE Database.session() call per /v1/agent/context-pack request, "
        f"got {sum(calls)}. #74 regression — telemetry should share the "
        f"handler's session, not open its own."
    )


# ─── #79: per-IP rate limit on /v1/search + /v1/agent/context-pack ──


@pytest.fixture
def rate_limited_client(database_url: str) -> Iterator[TestClient]:
    """A TestClient with the search/context-pack rate limit set to 1/min.

    Lets us prove the 4th request hits 429 with Retry-After without
    burning real capacity. We override env vars before building the
    app so the lifespan picks them up.
    """
    saved_url = os.environ.get("KILN_DATABASE_URL")
    saved_search = os.environ.get("KILN_RATE_LIMIT_SEARCH_PER_MIN")
    saved_feedback = os.environ.get("KILN_RATE_LIMIT_FEEDBACK_PER_MIN")
    os.environ["KILN_DATABASE_URL"] = database_url
    os.environ["KILN_RATE_LIMIT_SEARCH_PER_MIN"] = "1"
    os.environ["KILN_RATE_LIMIT_FEEDBACK_PER_MIN"] = "1"
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        for key, val in (
            ("KILN_DATABASE_URL", saved_url),
            ("KILN_RATE_LIMIT_SEARCH_PER_MIN", saved_search),
            ("KILN_RATE_LIMIT_FEEDBACK_PER_MIN", saved_feedback),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        get_settings.cache_clear()


def test_search_returns_429_when_rate_limited(rate_limited_client: TestClient) -> None:
    """#79: after the 1/min bucket drains, /v1/search returns 429."""
    body = {"query": "anything", "max_results": 1}
    # First call consumes the only token; status may be 200 or 503
    # depending on whether the embedding provider is configured. The
    # test only cares about the SECOND call, which is rate-limited
    # before any retrieval work runs.
    rate_limited_client.post("/v1/search", json=body)
    second = rate_limited_client.post("/v1/search", json=body)
    assert second.status_code == 429, second.text
    assert second.headers.get("retry-after") is not None
    assert int(second.headers["retry-after"]) >= 1


def test_context_pack_returns_429_when_rate_limited(rate_limited_client: TestClient) -> None:
    """#79: /v1/agent/context-pack shares the search limiter — also 429s."""
    body = {"task": "explain", "query": "anything"}
    rate_limited_client.post("/v1/agent/context-pack", json=body)
    second = rate_limited_client.post("/v1/agent/context-pack", json=body)
    assert second.status_code == 429, second.text
    assert int(second.headers["retry-after"]) >= 1
