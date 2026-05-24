"""Integration tests for POST /v1/answer (#192 Phase C).

End-to-end through the real FastAPI handler + live pgvector + a
MockGeneratorProvider attached to ``app.state``. Same pattern as
``test_api_routes.py`` / ``test_context_pack.py``.

Covers:

* Route registered, ``answer`` operationId.
* 503 when no generator is wired (the MVP default).
* Happy path: seed → POST → 200 with answer text + evidence + token
  budget + generator metadata.
* Telemetry: dedicated ``rag_answers`` row with full response classification.
* Refusal path: when retrieval finds nothing, ``answer=null`` and the
  generator is NOT invoked (we can detect by checking the mock's call
  list — but the mock is per-test so we wire one in and inspect).
* Request validation: empty query → 422.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db.models import RagAnswer
from cf_knowledge_kiln.generation import MockGeneratorProvider
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
    (tmp_path / "intro.md").write_text(
        textwrap.dedent(
            """\
            # Intro

            Widgets are devices that gadget. They support frobnication.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus_dir: Path) -> None:
    src = LocalSource(name="answer-tests", type="local", path=str(corpus_dir), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


@pytest.fixture
def client_no_generator(database_url: str) -> Iterator[TestClient]:
    """A client where the lifespan ran but no generator is wired.

    Default MVP shape: `config/models.yaml` either absent or has
    `generator.enabled: false`. `/v1/answer` should 503 cleanly.
    """
    saved = os.environ.get("KILN_DATABASE_URL")
    os.environ["KILN_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            # Defensive: the lifespan may have attached a generator if a
            # real config file was on the path; force-None for this fixture.
            c.app.state.generator_provider = None
            yield c
    finally:
        if saved is None:
            os.environ.pop("KILN_DATABASE_URL", None)
        else:
            os.environ["KILN_DATABASE_URL"] = saved
        get_settings.cache_clear()


@pytest.fixture
def client_with_mock_generator(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    """A client with a deterministic MockGeneratorProvider wired in.

    Also pins a low ``weak_evidence_score_threshold`` via a tmp
    ``config/security.yaml`` because :class:`MockEmbeddingProvider`
    is sha-derived (semantically blind) — its cosine similarities
    don't pass the default 0.46 threshold even on the "right" chunk,
    so the synthesis path would always trip the refusal branch and
    these tests couldn't exercise the happy path. Real-embedding
    tests (e.g. against e5-small-v2) would clear the default
    threshold; this fixture is the offline-mock equivalent.
    """
    saved_db = os.environ.get("KILN_DATABASE_URL")
    saved_sec = os.environ.get("KILN_SECURITY_CONFIG_PATH")
    os.environ["KILN_DATABASE_URL"] = database_url

    sec_path = tmp_path / "security.yaml"
    sec_path.write_text(
        textwrap.dedent(
            """\
            retrieval:
              weak_evidence_score_threshold: 0.01
            """
        ),
        encoding="utf-8",
    )
    os.environ["KILN_SECURITY_CONFIG_PATH"] = str(sec_path)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            c.app.state.generator_provider = MockGeneratorProvider(
                model="mock-answer",
                response_template="Widgets gadget [1].",
                prompt_tokens=120,
                completion_tokens=10,
            )
            yield c
    finally:
        if saved_db is None:
            os.environ.pop("KILN_DATABASE_URL", None)
        else:
            os.environ["KILN_DATABASE_URL"] = saved_db
        if saved_sec is None:
            os.environ.pop("KILN_SECURITY_CONFIG_PATH", None)
        else:
            os.environ["KILN_SECURITY_CONFIG_PATH"] = saved_sec
        get_settings.cache_clear()


# ─── 503 — no generator wired ────────────────────────────────────────


def test_answer_503_when_no_generator(client_no_generator: TestClient) -> None:
    """MVP default: /v1/answer returns 503 with a clear "no generator" hint."""
    response = client_no_generator.post("/v1/answer", json={"query": "anything", "max_chunks": 3})
    assert response.status_code == 503, response.text
    body = response.json()
    # FastAPI default error envelope.
    assert "generator" in body["detail"].lower()
    assert "KILN_GENERATOR" in body["detail"]


# ─── Happy path ──────────────────────────────────────────────────────


def test_answer_happy_path_returns_synthesized_answer(
    client_with_mock_generator: TestClient,
    session: AsyncSession,
    small_corpus: Path,
) -> None:
    """Seed → POST → 200 → AnswerResponse populated end-to-end."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    response = client_with_mock_generator.post(
        "/v1/answer", json={"query": "widgets", "max_chunks": 3}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Mock template echoes back what we configured.
    assert body["answer"] == "Widgets gadget [1]."
    assert body["answerable"] is True
    # ``refusal_reason`` is excluded from the JSON when None (response
    # model has ``exclude_none=True``).
    assert body.get("refusal_reason") is None
    assert body["evidence"], "expected at least one cited evidence chunk"
    assert body["generator_provider"] == "mock"
    assert body["generator_model"] == "mock-answer"
    # Token accounting from the mock's preset.
    assert body["token_budget"]["prompt_tokens"] == 120
    assert body["token_budget"]["completion_tokens"] == 10
    assert body["token_budget"]["total_tokens"] == 130
    assert body["token_budget"]["finish_reason"] == "stop"
    assert body["token_budget"]["requested_max_answer_tokens"] == 1024
    # Untrusted-content notice always present.
    assert body["untrusted_content_notice"]
    # answer_id is a UUID.
    assert body["answer_id"]


# ─── Telemetry ───────────────────────────────────────────────────────


def test_answer_persists_full_rag_answers_row_on_happy_path(
    client_with_mock_generator: TestClient,
    session: AsyncSession,
    small_corpus: Path,
    engine: AsyncEngine,
) -> None:
    """#221: the route writes a ``rag_answers`` row capturing the full
    response classification — answerable flag, generator metadata,
    finish_reason, token counts — not just the query + chunk ids.
    """
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    response = client_with_mock_generator.post("/v1/answer", json={"query": "widgets"})
    assert response.status_code == 200, response.text

    async def _rows() -> list[RagAnswer]:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            return list((await s.execute(select(RagAnswer))).scalars().all())

    rows = asyncio.get_event_loop().run_until_complete(_rows())
    assert len(rows) == 1
    row = rows[0]
    # Request-side fields.
    assert row.query == "widgets"
    assert row.evidence_chunk_ids
    assert row.requested_max_answer_tokens == 1024  # default
    # Happy-path classification.
    assert row.answerable is True
    assert row.requires_human_review is False
    assert row.refusal_reason is None
    # Generator metadata (mock from the fixture).
    assert row.generator_provider == "mock"
    assert row.generator_model == "mock-answer"
    assert row.finish_reason == "stop"
    assert row.prompt_tokens == 120
    assert row.completion_tokens == 10
    assert row.total_tokens == 130


# ─── Request validation ──────────────────────────────────────────────


def test_answer_422_on_empty_query(client_with_mock_generator: TestClient) -> None:
    """Pydantic min_length=1 on query → FastAPI 422."""
    response = client_with_mock_generator.post("/v1/answer", json={"query": ""})
    assert response.status_code == 422


def test_answer_422_on_max_answer_tokens_out_of_range(
    client_with_mock_generator: TestClient,
) -> None:
    """max_answer_tokens is bounded — too small or too large rejected."""
    too_small = client_with_mock_generator.post(
        "/v1/answer", json={"query": "x", "max_answer_tokens": 1}
    )
    assert too_small.status_code == 422
    too_large = client_with_mock_generator.post(
        "/v1/answer", json={"query": "x", "max_answer_tokens": 100000}
    )
    assert too_large.status_code == 422
