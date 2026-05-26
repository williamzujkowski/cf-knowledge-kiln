"""Regression test for #286 — dedup-check tx commits before embed phase.

#286 fingerprint: ``pg_stat_activity`` during a worker hang shows
one async-pool connection ``idle in transaction`` with the JOIN

    SELECT document_chunks.id, chunk_embeddings.content_hash
    FROM document_chunks JOIN chunk_embeddings ...

as the last query, two sibling connections post-COMMIT idle, worker
Python alive but no further logs and CPU at 0. The hang is
``embed_touched_documents`` running the dedup SELECT under SQLAlchemy's
autobegin (lazy transaction), then awaiting the minutes-long
CPU-bound embedding pass without committing — connection sits open
for the entire embed duration.

This test is unit-level (no DB): it instruments a mocked session to
record the call order and asserts ``session.commit`` ran AFTER the
dedup SELECT and BEFORE ``provider.embed_documents``. That ordering
is what guarantees the connection is not ``idle in transaction``
while the embedding model runs.

Companion to the integration test in
``tests/integration/test_ingestion_pipeline.py`` which exercises the
end-to-end commit boundary against a real Postgres.

Parallel-leak narrative: #245 closed the same idle-in-tx pattern at
the ``data_sources`` lock call site (catalog setup commits before
the embed phase). This test guards the dedup-check call site — a
DIFFERENT path with the same shape — so a future refactor can't
re-introduce a hang via the embedding pipeline.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cf_knowledge_kiln.ingestion._summary import IngestionSummary
from cf_knowledge_kiln.ingestion.embedding.pipeline import embed_touched_documents


class _CallOrderProvider:
    """Records the order in which ``embed_documents`` is invoked.

    ``embed_documents`` returns a single-dimension vector per text so
    the upsert path stays exercised; the test only asserts ordering.
    """

    provider = "order-recorder"
    model = "order-recorder-1"
    dimensions = 1

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._log.append("provider.embed_documents")
        return [[1.0] for _ in texts]

    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - unused
        return await self.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:  # pragma: no cover - unused
        return (await self.embed_documents([text]))[0]

    async def aclose(self) -> None:  # pragma: no cover - unused
        return None


def _chunk(doc_id: Any) -> Any:
    """Build a structural stand-in for ``DocumentChunk`` rows."""
    chunk = MagicMock()
    chunk.id = uuid4()
    chunk.document_id = doc_id
    chunk.content = "body"
    chunk.content_hash = "sha256:fresh"
    chunk.chunk_index = 0
    return chunk


class TestDedupTxCommitsBeforeEmbed:
    async def test_session_commit_runs_between_dedup_select_and_embed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix: commit fires AFTER ``_gather_chunks_needing_embed`` and BEFORE the embed pass.

        Without the commit, a real session would sit ``idle in
        transaction`` for the entire duration of
        ``provider.embed_documents`` — the #286 fingerprint.
        """
        doc_id = uuid4()
        chunk = _chunk(doc_id)
        log: list[str] = []

        # Mock session: every interesting call appends a marker so the
        # test can assert ordering of side effects.
        session = AsyncMock()
        session.flush = AsyncMock(side_effect=lambda: log.append("session.flush"))
        session.commit = AsyncMock(side_effect=lambda: log.append("session.commit"))
        session.rollback = AsyncMock()

        # Patch the two helpers the function calls so the test stays
        # focused on the commit boundary, not the SELECTs themselves.
        async def fake_gather(
            session_arg: Any, *, doc_ids: set[Any], repo: Any, summary: IngestionSummary
        ) -> list[Any]:
            log.append("dedup_select")
            return [chunk]

        async def fake_write(**kwargs: Any) -> None:
            log.append("write_embeddings")

        import cf_knowledge_kiln.ingestion.embedding.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "_gather_chunks_needing_embed", fake_gather)
        monkeypatch.setattr(pipeline_mod, "_write_embeddings", fake_write)

        provider = _CallOrderProvider(log)
        summary = IngestionSummary()

        await embed_touched_documents(
            session,
            doc_ids={doc_id},
            provider=provider,
            summary=summary,
            batch_size=8,
            concurrency=1,
        )

        # The commit must appear AFTER the dedup SELECT (so the
        # autobegin tx is closed) and BEFORE the provider call (so the
        # tx is NOT held across the minutes-long embed phase).
        assert "dedup_select" in log, f"dedup phase never ran; log={log}"
        assert "session.commit" in log, f"#286 regression: commit missing; log={log}"
        assert "provider.embed_documents" in log, f"embed phase never ran; log={log}"

        dedup_idx = log.index("dedup_select")
        commit_idx = log.index("session.commit")
        embed_idx = log.index("provider.embed_documents")
        assert dedup_idx < commit_idx < embed_idx, (
            "#286 regression: session.commit() must run between the dedup-check "
            f"SELECT and provider.embed_documents(). got order: {log}"
        )

    async def test_commit_runs_even_when_no_chunks_need_embedding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty ``to_embed`` still commits so pending chunk INSERTs don't leak the tx.

        The dedup SELECTs autobegin a transaction even when their
        result is empty. The chunk-INSERT work that ran in
        ``run_source`` BEFORE this function was called is also pending
        on the same tx (no commit between _process_file and
        embed_touched_documents). Returning early without commit would
        leave that tx open until the caller's terminal commit — for a
        short-corpus run that's harmless, but the commit boundary is
        a structural invariant we want to enforce uniformly so the
        empty-result fast path doesn't quietly diverge.
        """
        log: list[str] = []
        session = AsyncMock()
        session.flush = AsyncMock(side_effect=lambda: log.append("session.flush"))
        session.commit = AsyncMock(side_effect=lambda: log.append("session.commit"))

        async def fake_gather(*args: Any, **kwargs: Any) -> list[Any]:
            log.append("dedup_select_empty")
            return []

        import cf_knowledge_kiln.ingestion.embedding.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "_gather_chunks_needing_embed", fake_gather)

        provider = _CallOrderProvider(log)
        summary = IngestionSummary()

        await embed_touched_documents(
            session,
            doc_ids={uuid4()},
            provider=provider,
            summary=summary,
            batch_size=8,
            concurrency=1,
        )

        assert "session.commit" in log, (
            f"empty-dedup fast path must still commit the autobegin tx; log={log}"
        )
        assert "provider.embed_documents" not in log, (
            f"provider must not be called when no chunks need embedding; log={log}"
        )

    async def test_doc_ids_empty_short_circuits_without_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty ``doc_ids`` short-circuits before opening any tx — no commit needed.

        Guard against the obvious overcorrection: blindly committing
        on every call would mask bugs in the chunk pass. When
        ``doc_ids`` is empty, the function never touches the session,
        so no transaction is opened and no commit is required.
        """
        log: list[str] = []
        session = AsyncMock()
        session.flush = AsyncMock(side_effect=lambda: log.append("session.flush"))
        session.commit = AsyncMock(side_effect=lambda: log.append("session.commit"))

        provider = _CallOrderProvider(log)
        summary = IngestionSummary()

        await embed_touched_documents(
            session,
            doc_ids=set(),
            provider=provider,
            summary=summary,
            batch_size=8,
            concurrency=1,
        )

        assert log == [], f"empty doc_ids must not touch the session; got log={log}"
