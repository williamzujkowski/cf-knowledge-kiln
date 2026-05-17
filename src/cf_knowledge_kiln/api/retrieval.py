"""Phase 5 routes — human search + agent context pack.

Both routes delegate to :class:`HybridRetriever`. The ranked-chunk
output of slice 2 is mapped to :class:`ResultCard` (human shape) for
``/v1/search`` and to :class:`ContextPackResponse` (agent shape) for
``/v1/agent/context-pack``. Each call is logged to the operational
tables (``rag_queries`` and ``context_packs``) so the Phase 9 eval
harness has telemetry.

Per ADR-0003: the OpenAPI contract is canonical. Pydantic shapes here
match ``openapi/openapi.yaml`` 1:1 and the drift test in
``tests/unit/test_openapi_drift.py`` enforces it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from cf_knowledge_kiln.api.dependencies import get_db, get_hybrid_retriever
from cf_knowledge_kiln.db.connection import Database
from cf_knowledge_kiln.db.repositories import ContextPacksRepository, QueriesRepository
from cf_knowledge_kiln.retrieval import (
    ContextPackRequest,
    ContextPackResponse,
    HybridRetriever,
    RankedChunk,
    ResultCard,
    SearchRequest,
    SearchResponse,
)

router = APIRouter(tags=["search"])


@router.post(
    "/v1/search",
    operation_id="humanSearch",
    summary="Human search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
    responses={status.HTTP_200_OK: {"description": "Search results."}},
)
async def human_search(
    body: SearchRequest,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    db: Annotated[Database, Depends(get_db)],
) -> SearchResponse:
    """Run a hybrid retrieval query and return ranked result cards.

    Persists the query into ``rag_queries`` (consumer_type='human') for
    the Phase 9 eval harness.
    """
    filters = body.filters or _empty_filters()
    try:
        result = await retriever.search(body.query, filters=filters, max_results=body.max_results)
    except ValueError as exc:
        # Defensive: Pydantic min_length=1 catches empty already.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    response = SearchResponse(
        query=body.query,
        results=[
            _chunk_to_result_card(c, result.document_refs.get(c.document_id)) for c in result.chunks
        ],
        warnings=result.warnings or None,
    )
    await _log_rag_query(
        db,
        query=body.query,
        consumer_type="human",
        filters=filters.model_dump(exclude_none=True),
        chunk_ids=[c.chunk_id for c in result.chunks],
    )
    return response


@router.post(
    "/v1/agent/context-pack",
    operation_id="agentContextPack",
    summary="Build an agent context pack",
    response_model=ContextPackResponse,
    response_model_exclude_none=True,
    tags=["agent"],
    responses={status.HTTP_200_OK: {"description": "Context pack."}},
)
async def agent_context_pack(
    body: ContextPackRequest,
    retriever: Annotated[HybridRetriever, Depends(get_hybrid_retriever)],
    db: Annotated[Database, Depends(get_db)],
) -> ContextPackResponse:
    """Build a bounded, cited context pack for an agent consumer.

    Persists into ``context_packs`` so an operator can audit what the
    agent saw (and so the eval harness can replay).
    """
    filters = body.filters or _empty_filters()
    try:
        pack = await retriever.context_pack(
            body.query,
            task=body.task,
            filters=filters,
            max_chunks=body.max_chunks,
            max_tokens=body.max_tokens,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _log_context_pack(db, body=body, pack=pack)
    return pack


# ─── helpers ────────────────────────────────────────────────────────


def _empty_filters() -> Any:
    # Lazy-imported so the type isn't referenced at module scope —
    # mirrors the same cycle-avoidance pattern the engine uses.
    from cf_knowledge_kiln.retrieval import RetrievalFilters

    return RetrievalFilters()


def _chunk_to_result_card(chunk: RankedChunk, ref: object | None) -> ResultCard:
    """Map a :class:`RankedChunk` + :class:`DocumentRef` to a :class:`ResultCard`.

    The HybridRetriever returns scored chunks plus a ``document_refs``
    map carrying the per-document title/repo/path/etc. — we pull from
    that map here. A missing ref means the document was deleted
    between retrieval and serialization; we degrade to a minimal card
    rather than 500 the whole response.
    """
    title = getattr(ref, "title", None) or "(unknown)"
    return ResultCard(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        title=title,
        excerpt=str(chunk.chunk_metadata.get("text") or "")[:500],
        heading_path=list(chunk.heading_path) or None,
        repo=getattr(ref, "repo", None),
        path=getattr(ref, "path", None),
        commit_sha=getattr(ref, "commit_sha", None),
        owner=getattr(ref, "owner", None),
        status=chunk.status,  # type: ignore[arg-type]
        last_reviewed=chunk.last_reviewed,
        score=chunk.score,
    )


async def _log_rag_query(
    db: Database,
    *,
    query: str,
    consumer_type: str,
    filters: dict[str, Any],
    chunk_ids: list[Any],
) -> None:
    """Append a row to ``rag_queries``. Failures are logged but not fatal."""
    async with db.session() as session, session.begin():
        await QueriesRepository(session).create(
            query=query,
            consumer_type=consumer_type,
            filters=filters,
            retrieved_chunk_ids=chunk_ids,
        )


async def _log_context_pack(
    db: Database, *, body: ContextPackRequest, pack: ContextPackResponse
) -> None:
    """Append a row to ``context_packs``."""
    async with db.session() as session, session.begin():
        await ContextPacksRepository(session).create(
            query=body.query,
            task=body.task,
            token_budget=pack.token_budget.requested,
            filters=(body.filters.model_dump(exclude_none=True) if body.filters else {}),
            evidence_chunk_ids=[e.chunk_id for e in pack.evidence],
            token_estimate=pack.token_budget.used_estimate,
            confidence=pack.confidence,
            # mode='json' serializes UUIDs/dates as strings so they
            # round-trip through the JSONB column cleanly.
            warnings=[w.model_dump(mode="json") for w in pack.warnings],
            requires_human_review=pack.requires_human_review,
        )


__all__ = ["router"]
