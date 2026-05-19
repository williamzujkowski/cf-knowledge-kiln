"""SQL builders for Phase 5 hybrid retrieval (ADR-0009 §5).

Splitting the CTE-construction code out keeps
``db/repositories/documents.py`` under the 400-line budget. These
helpers are private to the ``db.repositories`` package — production
callers use :meth:`ChunksRepository.hybrid_search` and
:meth:`ChunksRepository.search_by_fts` instead.

The CTE shape is:

* **vec** — top-K chunk ids by ``embedding::vector(dim) <=> $1`` cosine
  distance, predicates pushed into the WHERE so the partial HNSW index
  is honored and irrelevant rows never enter the candidate pool.
* **fts** — top-K chunk ids by ``ts_rank_cd(to_tsvector, plainto_tsquery)``,
  same predicates pushed in.
* **fused** — ``SUM(1.0 / (k + rnk)) * (k + 1) / 2`` per chunk over the
  UNION ALL of the two arms, i.e., Reciprocal Rank Fusion with the
  output rescaled to ``[0, 1]`` (#164). A both-arm rank-1 hit normalizes
  to ``1.0``; a single-arm rank-1 hit to ``0.5``. Ordering is preserved
  (the scale factor is a positive constant per query). See ADR-0009.
* final SELECT — join fused back to documents + chunks for the row shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Float,
    bindparam,
    cast,
    func,
    literal_column,
    select,
    text,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.db.models import ChunkEmbedding, Document, DocumentChunk


@dataclass(frozen=True)
class SearchRow:
    """One row of hybrid-retrieval output, denormalized for the engine.

    Plain dataclass — not a SQLAlchemy ``Row`` — so the engine and the
    eventual context-pack serializer don't depend on the ORM layer.
    """

    chunk_id: UUID
    document_id: UUID
    content: str
    heading_path: tuple[str, ...]
    chunk_metadata: dict[str, Any]
    status: str
    authority: str | None
    owner: str | None
    last_reviewed: date | None
    commit_sha: str | None
    repo: str
    path: str
    title: str
    source_url: str | None
    score: float
    has_prompt_injection: bool
    has_sensitive_content: bool


async def set_local_ef_search(session: AsyncSession, ef_search: int) -> None:
    """``SET LOCAL hnsw.ef_search = N`` — must be inside a transaction.

    Postgres does not accept parameter binds for ``SET``, so we
    interpolate after coercing to ``int`` defensively.
    """
    safe = int(ef_search)
    if safe < 1:
        raise ValueError(f"hnsw.ef_search must be a positive int, got {ef_search!r}")
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {safe}"))


def build_hybrid_select(
    *,
    query_text: str,
    query_embedding: Sequence[float],
    dimensions: int,
    predicates: Sequence[Any],
    top_per_arm: int,
    final_limit: int,
    rrf_k: int,
) -> Any:
    """Compose the full vector+FTS CTE and return a final SELECT statement."""
    vec_cte = _vector_arm(query_embedding, dimensions, predicates, top_per_arm)
    fts_cte = _fts_arm(query_text, predicates, top_per_arm)
    # #164: rescale ``SUM(1/(k+rnk))`` by ``(k+1)/2`` so the fused score
    # lands in ``[0, 1]`` with both-arm rank-1 = 1.0. Ordering is
    # unchanged (positive constant per query), but the score is now
    # interpretable as "fraction of perfect-hit" — which lets the
    # downstream ``WEAK_EVIDENCE_SCORE_THRESHOLD`` (#160) and the
    # ``derive_confidence`` ``high`` cutoff at 0.8 (#164) actually fire.
    rrf_scale = (rrf_k + 1) / 2.0
    fused = (
        select(
            literal_column("u.chunk_id").label("chunk_id"),
            (func.sum(1.0 / (rrf_k + literal_column("u.rnk"))) * rrf_scale).label("rrf_score"),
        )
        .select_from(union_all(select(vec_cte), select(fts_cte)).alias("u"))
        .group_by(literal_column("u.chunk_id"))
        .subquery("fused")
    )
    return (
        _select_search_row_columns(cast(fused.c.rrf_score, Float).label("rrf_score"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(fused, fused.c.chunk_id == DocumentChunk.id)
        .order_by(fused.c.rrf_score.desc())
        .limit(final_limit)
    )


def build_fts_only_select(
    *,
    query_text: str,
    predicates: Sequence[Any],
    limit: int,
    rrf_k: int,
) -> Any:
    """FTS-only fallback (used when no embedding provider is configured).

    Applies the same ``(rrf_k + 1) / 2`` scale factor as
    :func:`build_hybrid_select` (#164). This is a pure unit conversion,
    not a renormalization of ``ts_rank_cd`` (which is unbounded): it
    keeps the weak-evidence threshold semantically equivalent across
    both paths. Pre-#164 the engine compared ``ts_rank_cd`` against the
    raw-scale ``0.015`` floor; post-#164 it compares ``ts_rank_cd * 30.5``
    against the normalized ``0.46`` floor — exact same gate.
    """
    tsv = func.to_tsvector("english", DocumentChunk.content)
    tsq = func.plainto_tsquery("english", query_text)
    ts_rank = func.ts_rank_cd(tsv, tsq)
    scale = (rrf_k + 1) / 2.0
    return (
        _select_search_row_columns((ts_rank * scale).label("rrf_score"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(tsv.op("@@")(tsq))
        .where(*predicates)
        .order_by(ts_rank.desc())
        .limit(limit)
    )


def _vector_arm(
    query_embedding: Sequence[float],
    dimensions: int,
    predicates: Sequence[Any],
    limit: int,
) -> Any:
    vec_type = Vector(dimensions)
    typed_col = cast(ChunkEmbedding.embedding, vec_type)
    qvec: Any = bindparam("qvec", value=list(query_embedding), type_=vec_type)
    distance = typed_col.op("<=>")(qvec)
    return (
        select(
            DocumentChunk.id.label("chunk_id"),
            func.row_number().over(order_by=distance.asc()).label("rnk"),
        )
        .select_from(ChunkEmbedding)
        .join(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(ChunkEmbedding.dimensions == dimensions)
        .where(*predicates)
        .order_by(distance.asc())
        .limit(limit)
        .cte("vec")
    )


def _fts_arm(query_text: str, predicates: Sequence[Any], limit: int) -> Any:
    tsv = func.to_tsvector("english", DocumentChunk.content)
    tsq = func.plainto_tsquery("english", query_text)
    ts_rank = func.ts_rank_cd(tsv, tsq)
    return (
        select(
            DocumentChunk.id.label("chunk_id"),
            func.row_number().over(order_by=ts_rank.desc()).label("rnk"),
        )
        .select_from(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(tsv.op("@@")(tsq))
        .where(*predicates)
        .order_by(ts_rank.desc())
        .limit(limit)
        .cte("fts")
    )


def _select_search_row_columns(score_col: Any) -> Any:
    """SELECT-list shared by hybrid + FTS-only paths."""
    return select(
        DocumentChunk.id.label("chunk_id"),
        DocumentChunk.document_id,
        DocumentChunk.content,
        DocumentChunk.heading_path,
        DocumentChunk.extra.label("chunk_metadata"),
        Document.status,
        Document.authority,
        Document.owner,
        Document.last_reviewed,
        Document.commit_sha,
        Document.repo,
        Document.path,
        Document.title,
        Document.source_url,
        score_col,
    ).select_from(DocumentChunk)


def row_to_search_row(row: Any) -> SearchRow:
    """Convert a SQLAlchemy ``RowMapping`` into a :class:`SearchRow`."""
    metadata = dict(row["chunk_metadata"] or {})
    heading = tuple(row["heading_path"] or ())
    return SearchRow(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        content=row["content"],
        heading_path=heading,
        chunk_metadata=metadata,
        status=row["status"],
        authority=row["authority"],
        owner=row["owner"],
        last_reviewed=row["last_reviewed"],
        commit_sha=row["commit_sha"],
        repo=row["repo"],
        path=row["path"],
        title=row["title"],
        source_url=row["source_url"],
        score=float(row["rrf_score"]),
        has_prompt_injection=bool(metadata.get("has_prompt_injection")),
        has_sensitive_content=bool(metadata.get("has_sensitive_content")),
    )


__all__ = [
    "SearchRow",
    "build_fts_only_select",
    "build_hybrid_select",
    "row_to_search_row",
    "set_local_ef_search",
]
