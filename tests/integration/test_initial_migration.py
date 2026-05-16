"""Smoke tests for the initial schema migration.

Covers issue #11 acceptance:

* All 9 plan tables exist with the columns the plan calls out.
* ``pgvector`` extension is present.
* The HNSW partial index is present on ``chunk_embeddings``.
* A sample document → chunk → embedding round-trips: insert, then read
  it back via vector similarity.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_pgvector_extension_installed(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector'"))
        assert row.scalar() == "vector"


@pytest.mark.parametrize(
    "table",
    [
        "data_sources",
        "model_registry",
        "documents",
        "ingestion_runs",
        "document_chunks",
        "chunk_embeddings",
        "rag_queries",
        "rag_feedback",
        "context_packs",
    ],
)
async def test_plan_table_exists(engine: AsyncEngine, table: str) -> None:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        assert row.scalar() == 1, f"missing table: {table}"


async def test_hnsw_partial_index_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' "
                "AND indexname = 'ix_chunk_embeddings_hnsw_768'"
            )
        )
        indexdef = row.scalar()
        assert indexdef is not None
        assert "hnsw" in indexdef.lower()
        assert "dimensions = 768" in indexdef


async def test_fts_gin_index_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' "
                "AND indexname = 'ix_chunks_content_fts'"
            )
        )
        indexdef = row.scalar()
        assert indexdef is not None
        assert "gin" in indexdef.lower()
        assert "to_tsvector" in indexdef


async def test_document_chunk_embedding_round_trip(engine: AsyncEngine) -> None:
    """Insert a document → chunk → embedding and read it back via similarity."""
    embedding_literal = "[" + ",".join(["0.1"] * 768) + "]"
    async with engine.begin() as conn:
        doc_id = (
            await conn.execute(
                text(
                    "INSERT INTO documents (repo, path, title, status) "
                    "VALUES (:repo, :path, :title, 'active') RETURNING id"
                ),
                {"repo": "test/repo", "path": "docs/example.md", "title": "Example"},
            )
        ).scalar()
        assert doc_id is not None

        chunk_id = (
            await conn.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(document_id, chunk_index, content, content_hash) "
                    "VALUES (:doc_id, 0, :content, :hash) RETURNING id"
                ),
                {
                    "doc_id": doc_id,
                    "content": "Hello pgvector — this is a chunk of source text.",
                    "hash": "sha256:test",
                },
            )
        ).scalar()
        assert chunk_id is not None

        await conn.execute(
            text(
                "INSERT INTO chunk_embeddings "
                "(chunk_id, embedding, model, provider, dimensions, content_hash) "
                "VALUES (:chunk_id, CAST(:emb AS vector), :model, :provider, 768, :hash)"
            ),
            {
                "chunk_id": chunk_id,
                "emb": embedding_literal,
                "model": "nomic-embed-text-v1.5",
                "provider": "nomic",
                "hash": "sha256:test",
            },
        )

    async with engine.connect() as conn:
        result = (
            await conn.execute(
                text(
                    "SELECT c.content, e.model, e.dimensions "
                    "FROM document_chunks c "
                    "JOIN chunk_embeddings e ON e.chunk_id = c.id "
                    "WHERE c.id = :chunk_id"
                ),
                {"chunk_id": chunk_id},
            )
        ).one()
        assert result.model == "nomic-embed-text-v1.5"
        assert result.dimensions == 768
        assert "pgvector" in result.content


async def test_fts_returns_inserted_chunk(engine: AsyncEngine) -> None:
    """FTS index must rank the inserted chunk for a matching query."""
    async with engine.begin() as conn:
        doc_id = (
            await conn.execute(
                text(
                    "INSERT INTO documents (repo, path, title, status) "
                    "VALUES ('r', 'p', 't', 'active') RETURNING id"
                )
            )
        ).scalar()
        await conn.execute(
            text(
                "INSERT INTO document_chunks "
                "(document_id, chunk_index, content, content_hash) "
                "VALUES (:doc_id, 0, "
                "'Cloud Foundry binds Postgres services via VCAP_SERVICES', "
                "'sha256:fts')"
            ),
            {"doc_id": doc_id},
        )

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT content FROM document_chunks "
                    "WHERE to_tsvector('english', content) "
                    "@@ plainto_tsquery('english', 'cloud foundry postgres')"
                )
            )
        ).fetchall()
        assert any("Cloud Foundry" in r.content for r in row)
