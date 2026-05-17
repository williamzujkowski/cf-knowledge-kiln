"""Integration tests for the Phase 6 HTMX search UI (issue #23).

End-to-end via the FastAPI lifespan + live pgvector DB. Asserts:

* ``GET /`` returns the search shell with a form
* ``POST /search`` returns a results-list HTML fragment (HTMX target)
* Filters round-trip from form to RetrievalFilters to engine
* Deprecated results carry the visible flag (per AGENTS.md)
* Empty query returns the empty-state fragment, not a 500
* The static CSS file mounts at /static/kiln.css
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db.models import Document, RagQuery
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
    src = LocalSource(name="web-tests", type="local", path=str(corpus_dir), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
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


# ─── GET / ──────────────────────────────────────────────────────────


def test_search_page_renders_form(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # Form points at /search with HTMX swap on #results.
    assert 'hx-post="/search"' in body
    assert 'hx-target="#results"' in body
    assert 'name="query"' in body
    # No HTML-escaping bugs on the empty-state hint.
    assert "Type a query above to search." in body


def test_static_css_is_served(client: TestClient) -> None:
    response = client.get("/static/kiln.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "result-card" in response.text  # sanity: real CSS body


# ─── POST /search (HTMX fragment) ───────────────────────────────────


def test_search_post_returns_results_fragment(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "widgets", "status": ["active"]})
    assert response.status_code == 200
    body = response.text
    # Fragment is a partial — no <html>, <body>, or <header>.
    assert "<html" not in body
    # Result card structure renders.
    assert "results-list" in body
    assert "result-card" in body
    assert "Beta" in body  # title of the only doc matching 'widgets'


def test_search_post_empty_query_returns_empty_fragment(client: TestClient) -> None:
    response = client.post("/search", data={"query": "  "})
    assert response.status_code == 200
    assert "result-card" not in response.text


def test_search_post_returns_empty_results_message_on_no_match(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post(
        "/search", data={"query": "completelynonexistentterm", "status": ["active"]}
    )
    assert response.status_code == 200
    assert "No results" in response.text


# ─── Deprecated flagging (AGENTS.md) ────────────────────────────────


def test_deprecated_results_carry_visible_flag(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """Deprecated docs must be visibly different — CSS class + status badge."""
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    # Demote beta to deprecated.
    asyncio.get_event_loop().run_until_complete(_set_status(session, "beta.md", "deprecated"))

    response = client.post(
        "/search",
        # Allow deprecated through so it actually surfaces.
        data={"query": "widgets", "status": ["active", "deprecated"]},
    )
    assert response.status_code == 200
    body = response.text
    # CSS hook on the card itself.
    assert "status-deprecated" in body
    # Status badge text.
    assert ">deprecated<" in body
    # Reviewer-warned: also surfaces a deprecated_source warning.
    assert "warning-deprecated_source" in body


async def _set_status(session: AsyncSession, path: str, new_status: str) -> None:
    await session.execute(update(Document).where(Document.path == path).values(status=new_status))
    await session.commit()


# ─── Telemetry persistence ──────────────────────────────────────────


def test_web_search_persists_rag_query_with_human_consumer(
    client: TestClient, session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "widgets", "status": ["active"]})
    assert response.status_code == 200

    async def _rows() -> list[RagQuery]:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            return list((await s.execute(select(RagQuery))).scalars().all())

    rows = asyncio.get_event_loop().run_until_complete(_rows())
    assert len(rows) == 1
    assert rows[0].consumer_type == "human"
    assert rows[0].query == "widgets"
