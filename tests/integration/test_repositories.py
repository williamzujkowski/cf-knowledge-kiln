"""Integration tests for each repository (issue #12).

Each repo gets at least create + get + list (with a filter) + delete.
Tests scope themselves to one repo's tables, per the acceptance rule:
*"No test reaches into another repository's tables."* When a foreign
key is required, the test uses the corresponding repository to mint the
parent row — never raw SQL into another repo's table.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.db.repositories import (
    AnswersRepository,
    ChunksRepository,
    ContextPacksRepository,
    DataSourcesRepository,
    DocumentsRepository,
    EmbeddingsRepository,
    FeedbackRepository,
    IngestionJobsRepository,
    IngestionRunsRepository,
    ModelRegistryRepository,
    QueriesRepository,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


# ─── data_sources ───────────────────────────────────────────────────


async def test_data_sources_crud(session: AsyncSession) -> None:
    repo = DataSourcesRepository(session)
    created = await repo.create(
        name="example-repo", type="git", location="https://example.invalid/r.git"
    )
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.name == "example-repo"

    await repo.create(name="paused-repo", type="git", location="x", status="paused")
    active_only = await repo.list(status="active")
    assert {row.name for row in active_only} == {"example-repo"}

    await repo.delete(created.id)
    assert await repo.get(created.id) is None


# ─── model_registry ─────────────────────────────────────────────────


async def test_model_registry_crud(session: AsyncSession) -> None:
    repo = ModelRegistryRepository(session)
    a = await repo.create(
        kind="embedding",
        provider="nomic",
        name="nomic-embed-text-v1.5",
        dimensions=768,
        enabled=True,
    )
    await repo.create(
        kind="generator",
        provider="openai-compatible",
        name="phi-4-mini-instruct",
        enabled=False,
    )

    embeddings = await repo.list(kind="embedding")
    assert {row.name for row in embeddings} == {"nomic-embed-text-v1.5"}

    enabled = await repo.list(enabled=True)
    assert {row.id for row in enabled} == {a.id}

    await repo.delete(a.id)
    assert await repo.get(a.id) is None


# ─── documents ──────────────────────────────────────────────────────


async def test_documents_crud(session: AsyncSession) -> None:
    repo = DocumentsRepository(session)
    a = await repo.create(repo="org/handbook", path="security/access.md", title="Access")
    await repo.create(
        repo="org/handbook",
        path="ops/deploy.md",
        title="Deploy",
        doc_type="runbook",
        status="deprecated",
    )

    active = await repo.list(status="active")
    assert {row.path for row in active} == {"security/access.md"}

    runbooks = await repo.list(doc_type="runbook")
    assert {row.path for row in runbooks} == {"ops/deploy.md"}

    await repo.delete(a.id)
    assert await repo.get(a.id) is None


# ─── document_chunks ────────────────────────────────────────────────


async def test_chunks_crud(session: AsyncSession) -> None:
    docs = DocumentsRepository(session)
    chunks = ChunksRepository(session)

    parent = await docs.create(repo="r", path="p", title="t")
    chunk_a = await chunks.create(
        document_id=parent.id,
        chunk_index=0,
        content="Chunk A",
        content_hash="sha256:a",
        heading_path=["Intro"],
    )
    await chunks.create(
        document_id=parent.id,
        chunk_index=1,
        content="Chunk B",
        content_hash="sha256:b",
    )

    all_for_doc = await chunks.list(document_id=parent.id)
    assert [c.chunk_index for c in all_for_doc] == [0, 1]

    matched = await chunks.list(content_hash="sha256:a")
    assert {c.id for c in matched} == {chunk_a.id}

    await chunks.delete(chunk_a.id)
    assert await chunks.get(chunk_a.id) is None


# ─── chunk_embeddings ───────────────────────────────────────────────


async def test_embeddings_crud(session: AsyncSession) -> None:
    docs = DocumentsRepository(session)
    chunks = ChunksRepository(session)
    embeds = EmbeddingsRepository(session)

    doc = await docs.create(repo="r", path="p", title="t")
    chunk = await chunks.create(document_id=doc.id, chunk_index=0, content="x", content_hash="h")
    vec_768 = [0.1] * 768
    created = await embeds.create(
        chunk_id=chunk.id,
        embedding=vec_768,
        model="nomic-embed-text-v1.5",
        provider="nomic",
        dimensions=768,
        content_hash="h",
    )
    assert created.dimensions == 768

    fetched = await embeds.get(chunk.id)
    assert fetched is not None
    assert fetched.model == "nomic-embed-text-v1.5"

    by_dim = await embeds.list(dimensions=768)
    assert {e.chunk_id for e in by_dim} == {chunk.id}

    await embeds.delete(chunk.id)
    assert await embeds.get(chunk.id) is None


async def test_embeddings_upsert_replaces_existing(session: AsyncSession) -> None:
    """Issue #18: re-embedding a chunk overwrites the row, not duplicates it."""
    docs = DocumentsRepository(session)
    chunks = ChunksRepository(session)
    embeds = EmbeddingsRepository(session)

    doc = await docs.create(repo="r", path="p", title="t")
    chunk = await chunks.create(
        document_id=doc.id, chunk_index=0, content="x", content_hash="hash-v1"
    )
    await embeds.upsert(
        chunk_id=chunk.id,
        embedding=[0.1] * 768,
        model="nomic-embed-text-v1.5",
        provider="local",
        dimensions=768,
        content_hash="hash-v1",
    )
    # Same chunk, same model — but content rewritten, so new vector + hash.
    await embeds.upsert(
        chunk_id=chunk.id,
        embedding=[0.5] * 768,
        model="nomic-embed-text-v1.5",
        provider="local",
        dimensions=768,
        content_hash="hash-v2",
    )

    rows = await embeds.list(dimensions=768)
    assert len(rows) == 1
    assert rows[0].content_hash == "hash-v2"


async def test_embeddings_existing_hashes_for_document(session: AsyncSession) -> None:
    """Issue #18: the pipeline needs the {chunk_id: hash} map to decide skips."""
    docs = DocumentsRepository(session)
    chunks = ChunksRepository(session)
    embeds = EmbeddingsRepository(session)

    doc = await docs.create(repo="r", path="p", title="t")
    chunk_embedded = await chunks.create(
        document_id=doc.id, chunk_index=0, content="x", content_hash="h0"
    )
    chunk_pending = await chunks.create(
        document_id=doc.id, chunk_index=1, content="y", content_hash="h1"
    )
    # Only one of the two chunks has an embedding row.
    await embeds.upsert(
        chunk_id=chunk_embedded.id,
        embedding=[0.0] * 768,
        model="m",
        provider="mock",
        dimensions=768,
        content_hash="h0",
    )

    hashes = await embeds.existing_hashes_for_document(doc.id)
    assert hashes == {chunk_embedded.id: "h0"}
    assert chunk_pending.id not in hashes


# ─── ingestion_runs ─────────────────────────────────────────────────


async def test_ingestion_runs_crud(session: AsyncSession) -> None:
    sources = DataSourcesRepository(session)
    runs = IngestionRunsRepository(session)

    src = await sources.create(name="s", type="git", location="x")
    running = await runs.create(source_id=src.id, status="running")
    await runs.create(source_id=src.id, status="succeeded", stats={"chunks": 7})

    for_source = await runs.list(source_id=src.id)
    assert len(for_source) == 2

    succeeded = await runs.list(status="succeeded")
    assert {r.status for r in succeeded} == {"succeeded"}

    await runs.delete(running.id)
    assert await runs.get(running.id) is None


# ─── rag_queries ────────────────────────────────────────────────────


async def test_queries_crud(session: AsyncSession) -> None:
    repo = QueriesRepository(session)
    q = await repo.create(query="hello", consumer_type="human", requester="alice")
    await repo.create(query="agent-q", consumer_type="agent")

    humans = await repo.list(consumer_type="human")
    assert {row.id for row in humans} == {q.id}

    await repo.delete(q.id)
    assert await repo.get(q.id) is None


async def test_queries_list_since_filter(session: AsyncSession) -> None:
    """#54: ``list(since=...)`` is a >= filter on created_at.

    Regression cover for an untested predicate. Each row's
    ``created_at`` is Postgres ``now()``, which is constant within a
    transaction — commit between creates so the two rows get distinct
    timestamps and the cutoff actually splits them.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select as sa_select

    from cf_knowledge_kiln.db.models import RagQuery

    repo = QueriesRepository(session)
    older = await repo.create(query="older", consumer_type="human")
    await session.commit()
    # Take cutoff from the DB itself, not Python's wall clock — avoids
    # any client/server skew. cutoff > older.created_at by definition.
    older_row = (
        await session.execute(sa_select(RagQuery).where(RagQuery.id == older.id))
    ).scalar_one()
    cutoff = older_row.created_at + timedelta(microseconds=1)
    await asyncio.sleep(0.01)
    newer = await repo.create(query="newer", consumer_type="human")
    await session.commit()

    after_cutoff = await repo.list(since=cutoff)
    after_cutoff_ids = {row.id for row in after_cutoff}
    assert newer.id in after_cutoff_ids
    assert older.id not in after_cutoff_ids

    # Far-future cutoff matches nothing.
    none_after = await repo.list(since=datetime.now(UTC) + timedelta(days=1))
    assert none_after == []


# ─── rag_feedback ───────────────────────────────────────────────────


async def test_feedback_crud(session: AsyncSession) -> None:
    queries = QueriesRepository(session)
    feedback = FeedbackRepository(session)

    q = await queries.create(query="hello", consumer_type="human")
    f = await feedback.create(query_id=q.id, signal="helpful", source="ui")
    await feedback.create(query_id=q.id, signal="not_helpful")

    helpful = await feedback.list(signal="helpful")
    assert {row.id for row in helpful} == {f.id}

    for_query = await feedback.list(query_id=q.id)
    assert len(for_query) == 2

    await feedback.delete(f.id)
    assert await feedback.get(f.id) is None


# ─── context_packs ──────────────────────────────────────────────────


async def test_context_packs_crud(session: AsyncSession) -> None:
    repo = ContextPacksRepository(session)
    a = await repo.create(query="q", task="t", token_budget=3000)
    await repo.create(
        query="q2",
        task="t2",
        token_budget=3000,
        requires_human_review=True,
    )

    flagged = await repo.list(requires_human_review=True)
    assert {row.query for row in flagged} == {"q2"}

    await repo.delete(a.id)
    assert await repo.get(a.id) is None


# ─── ingestion_jobs mutations ───────────────────────────────────────


async def test_jobs_mark_done_sets_terminal_state(session: AsyncSession) -> None:
    """Issue #52: mark_done transitions a job to succeeded + sets finished_at."""
    repo = IngestionJobsRepository(session)
    job = await repo.create(payload={"source_name": "test"})
    # Claim once so it's `running` — matches the worker's real-life path.
    claimed = await repo.claim_one()
    assert claimed is not None and claimed.id == job.id

    await repo.mark_done(job.id, result_run_id=None)
    await session.commit()
    await session.refresh(claimed)

    assert claimed.status == "succeeded"
    assert claimed.finished_at is not None
    assert claimed.last_error is None


async def test_jobs_mark_failed_records_error_and_finished_at(
    session: AsyncSession,
) -> None:
    """Issue #52: mark_failed transitions to failed + persists the error."""
    repo = IngestionJobsRepository(session)
    job = await repo.create(payload={"source_name": "test"})
    claimed = await repo.claim_one()
    assert claimed is not None

    await repo.mark_failed(job.id, error="connector exploded mid-fetch")
    await session.commit()
    await session.refresh(claimed)

    assert claimed.status == "failed"
    assert claimed.last_error == "connector exploded mid-fetch"
    assert claimed.finished_at is not None


async def test_jobs_requeue_clears_timestamps_and_error(session: AsyncSession) -> None:
    """Issue #52: requeue rewinds a job for re-processing without losing
    the attempt count (which the recovery sweep relies on as a forensic
    breadcrumb)."""
    repo = IngestionJobsRepository(session)
    job = await repo.create(payload={"source_name": "test"})
    claimed = await repo.claim_one()  # status -> running, attempts++
    assert claimed is not None
    await repo.mark_failed(job.id, error="transient failure")
    await session.commit()
    await session.refresh(claimed)
    assert claimed.attempts == 1
    assert claimed.status == "failed"

    await repo.requeue(job.id)
    await session.commit()
    await session.refresh(claimed)

    assert claimed.status == "queued"
    assert claimed.started_at is None
    assert claimed.finished_at is None
    assert claimed.last_error is None
    # attempts intentionally NOT reset — the next claim bumps it.
    assert claimed.attempts == 1


async def test_jobs_mark_done_with_result_run_id_links_back(
    session: AsyncSession,
) -> None:
    """When a Worker completes a job it points result_run_id at the
    ingestion_runs row, so an operator can trace the work."""
    runs = IngestionRunsRepository(session)
    jobs = IngestionJobsRepository(session)

    job = await jobs.create(payload={"source_name": "test"})
    claimed = await jobs.claim_one()
    assert claimed is not None
    run = await runs.create(status="succeeded")
    await session.flush()

    await jobs.mark_done(job.id, result_run_id=run.id)
    await session.commit()
    await session.refresh(claimed)

    assert claimed.result_run_id == run.id


# ─── AnswersRepository (#221) ─────────────────────────────────────────


async def test_answers_crud_happy_path(session: AsyncSession) -> None:
    """#221: a successful answer round-trips every field."""
    repo = AnswersRepository(session)
    row = await repo.create(
        query="what is widget X",
        task="explain widget X",
        evidence_chunk_ids=[uuid4()],
        answerable=True,
        requires_human_review=False,
        requested_max_answer_tokens=1024,
        confidence="high",
        generator_provider="openai-compatible",
        generator_model="phi-4-mini-instruct",
        finish_reason="stop",
        prompt_tokens=120,
        completion_tokens=10,
        total_tokens=130,
    )
    fetched = await repo.get(row.id)
    assert fetched is not None
    assert fetched.query == "what is widget X"
    assert fetched.task == "explain widget X"
    assert fetched.answerable is True
    assert fetched.requires_human_review is False
    assert fetched.refusal_reason is None
    assert fetched.generator_model == "phi-4-mini-instruct"
    assert fetched.finish_reason == "stop"
    assert fetched.total_tokens == 130
    await repo.delete(row.id)
    assert await repo.get(row.id) is None


async def test_answers_crud_refusal_path(session: AsyncSession) -> None:
    """#221: a refusal stores null generator metadata + a reason."""
    repo = AnswersRepository(session)
    row = await repo.create(
        query="anything",
        answerable=False,
        requires_human_review=True,
        requested_max_answer_tokens=1024,
        refusal_reason="no evidence found for the query",
        confidence="none",
        # Generator never reached — all nulls.
        generator_provider=None,
        generator_model=None,
        finish_reason=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )
    assert row.refusal_reason == "no evidence found for the query"
    assert row.generator_provider is None
    assert row.prompt_tokens is None


async def test_answers_list_filters(session: AsyncSession) -> None:
    """``list`` applies answerable / requires_human_review / generator_model filters."""
    repo = AnswersRepository(session)
    await repo.create(
        query="happy",
        answerable=True,
        requires_human_review=False,
        requested_max_answer_tokens=512,
        generator_model="phi-4",
    )
    await repo.create(
        query="refused",
        answerable=False,
        requires_human_review=True,
        requested_max_answer_tokens=512,
    )
    await repo.create(
        query="other model",
        answerable=True,
        requires_human_review=False,
        requested_max_answer_tokens=512,
        generator_model="llama-3",
    )

    answerable = await repo.list(answerable=True)
    assert {r.query for r in answerable} == {"happy", "other model"}

    review = await repo.list(requires_human_review=True)
    assert {r.query for r in review} == {"refused"}

    phi = await repo.list(generator_model="phi-4")
    assert {r.query for r in phi} == {"happy"}


async def test_answers_negative_token_count_rejected(session: AsyncSession) -> None:
    """CHECK constraint rejects negative token counts (data-integrity belt)."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    repo = AnswersRepository(session)
    with pytest.raises(IntegrityError):
        await repo.create(
            query="x",
            answerable=True,
            requires_human_review=False,
            requested_max_answer_tokens=512,
            prompt_tokens=-1,  # constraint violation
        )
        await session.flush()
