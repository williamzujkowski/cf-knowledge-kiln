"""Integration tests for the feedback UI (Phase 6 issue #25).

Each /search response includes a query_id; the per-result feedback
widget posts (query_id, chunk_id, signal, optional comment) to
POST /feedback, which appends a rag_feedback row and HTMX-swaps in
an acknowledgement chip.
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db.models import RagFeedback, RagQuery
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
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text(
        textwrap.dedent(
            """\
            # Alpha
            widgets and gadgets and rivets.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus_dir: Path) -> None:
    src = LocalSource(name="fb-tests", type="local", path=str(corpus_dir), include=["**/*.md"])
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


def _do_search_and_extract_ids(
    client: TestClient, session: AsyncSession, corpus: Path, query: str = "widgets"
) -> tuple[str, str]:
    """Seed → POST /search → pull query_id + first chunk_id from the HTML."""
    asyncio.get_event_loop().run_until_complete(_seed(session, corpus))
    response = client.post(
        "/search", data={"query": query, "_filters_set": "1", "status": "active"}
    )
    assert response.status_code == 200, response.text
    body = response.text
    # query_id is rendered as a hidden input value in the feedback form.
    # chunk_id is rendered both as data-document-id on the card and as
    # a hidden input in the feedback form. Pull from the form to keep
    # things tightly coupled to the actual contract.
    import re

    qid_match = re.search(r'name="query_id" value="([0-9a-f-]+)"', body)
    cid_match = re.search(r'name="chunk_id" value="([0-9a-f-]+)"', body)
    assert qid_match, "expected query_id hidden field in feedback widget"
    assert cid_match, "expected chunk_id hidden field in feedback widget"
    return qid_match.group(1), cid_match.group(1)


# ─── Acceptance: form submission persists + returns ack ─────────────


def test_feedback_persists_row_and_returns_ack(
    client: TestClient, session: AsyncSession, corpus: Path, engine: AsyncEngine
) -> None:
    qid, cid = _do_search_and_extract_ids(client, session, corpus)
    r = client.post(
        "/feedback",
        data={
            "query_id": qid,
            "chunk_id": cid,
            "signal": "useful",
            "comment": "matched what I needed",
        },
    )
    assert r.status_code == 200, r.text
    # #340 editorial copy: 'Thanks' replaced by italic 'Noted'.
    # See docs/copy-voice.md for the canonical phrase.
    assert "Noted" in r.text
    assert "useful" in r.text

    async def _rows() -> list[RagFeedback]:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            return list((await s.execute(select(RagFeedback))).scalars().all())

    rows = asyncio.get_event_loop().run_until_complete(_rows())
    assert len(rows) == 1
    row = rows[0]
    assert row.signal == "useful"
    assert row.comment == "matched what I needed"
    assert row.source == "web"
    assert str(row.query_id) == qid
    assert str(row.chunk_id) == cid


# ─── All six signal types accepted ──────────────────────────────────


@pytest.mark.parametrize(
    "signal",
    [
        "useful",
        "not_useful",
        "stale",
        "wrong_source",
        "missing_source",
        "duplicate_or_conflicting",
    ],
)
def test_feedback_accepts_each_documented_signal(
    client: TestClient,
    session: AsyncSession,
    corpus: Path,
    engine: AsyncEngine,
    signal: str,
) -> None:
    qid, cid = _do_search_and_extract_ids(client, session, corpus)
    r = client.post("/feedback", data={"query_id": qid, "chunk_id": cid, "signal": signal})
    assert r.status_code == 200, r.text
    assert signal.replace("_", " ") in r.text


# ─── Validation ─────────────────────────────────────────────────────


def test_feedback_rejects_unknown_signal(
    client: TestClient, session: AsyncSession, corpus: Path
) -> None:
    qid, cid = _do_search_and_extract_ids(client, session, corpus)
    r = client.post(
        "/feedback",
        data={"query_id": qid, "chunk_id": cid, "signal": "thumbs_sideways"},
    )
    assert r.status_code == 400
    assert "Unknown feedback type" in r.text


def test_feedback_rejects_non_uuid_ids(client: TestClient) -> None:
    r = client.post(
        "/feedback",
        data={"query_id": "not-a-uuid", "chunk_id": str(uuid4()), "signal": "useful"},
    )
    assert r.status_code == 400
    assert "Invalid" in r.text


def test_feedback_truncates_comment_to_500_chars(
    client: TestClient, session: AsyncSession, corpus: Path, engine: AsyncEngine
) -> None:
    qid, cid = _do_search_and_extract_ids(client, session, corpus)
    long = "x" * 2000
    r = client.post(
        "/feedback",
        data={"query_id": qid, "chunk_id": cid, "signal": "useful", "comment": long},
    )
    assert r.status_code == 200

    async def _comment() -> str | None:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            row = (await s.execute(select(RagFeedback))).scalar_one()
            return row.comment

    c = asyncio.get_event_loop().run_until_complete(_comment())
    assert c is not None
    assert len(c) == 500


# ─── Search-page rendering ──────────────────────────────────────────


def test_search_response_renders_feedback_widget_per_result(
    client: TestClient, session: AsyncSession, corpus: Path
) -> None:
    asyncio.get_event_loop().run_until_complete(_seed(session, corpus))
    r = client.post("/search", data={"query": "widgets", "_filters_set": "1", "status": "active"})
    assert r.status_code == 200
    # Widget contains the hidden form fields, a radio for each signal,
    # and the submit button.
    body = r.text
    assert 'hx-post="/feedback"' in body
    assert 'name="signal"' in body
    for sig in (
        "useful",
        "not_useful",
        "stale",
        "wrong_source",
        "missing_source",
        "duplicate_or_conflicting",
    ):
        assert f'value="{sig}"' in body


def test_search_persists_query_id_used_by_feedback_widget(
    client: TestClient, session: AsyncSession, corpus: Path, engine: AsyncEngine
) -> None:
    """Acceptance: a rag_queries row is written and surfaces as query_id."""
    qid, _cid = _do_search_and_extract_ids(client, session, corpus)

    async def _ids() -> list[str]:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            rows = (await s.execute(select(RagQuery.id))).scalars().all()
            return [str(i) for i in rows]

    ids = asyncio.get_event_loop().run_until_complete(_ids())
    assert qid in ids
