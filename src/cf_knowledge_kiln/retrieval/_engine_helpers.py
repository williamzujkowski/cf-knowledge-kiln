"""Pure-logic helpers for :mod:`cf_knowledge_kiln.retrieval.engine`.

Extracted from ``engine.py`` (#197) so the orchestration class
(``HybridRetriever``, with its spans + session machinery) stays the
hot file readers visit first. Nothing here touches the database or
the embedding provider — every function is a pure data shape
transformation or a thin re-grouping of the ``ranking`` warning
emitters.

Private module: leading underscore, mirrored ``__all__``, no
re-export from ``retrieval/__init__.py``. Call sites are
``engine.py`` only.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from cf_knowledge_kiln.db.repositories._hybrid import SearchRow
from cf_knowledge_kiln.retrieval.ranking import (
    RankedChunk,
    deprecated_warnings,
    isolated_match_warning,
    prompt_injection_warnings,
    sensitive_content_warnings,
    stale_warnings,
    weak_evidence_warning,
)
from cf_knowledge_kiln.retrieval.types import Conflict, Warning
from cf_knowledge_kiln.retrieval.warning_variants import (
    ConflictingSourcesWarning,
    QueryNormalizedWarning,
    downgrade_to_flat,
)


def row_to_ranked_chunk(row: SearchRow) -> RankedChunk:
    return RankedChunk(
        chunk_id=row.chunk_id,
        document_id=row.document_id,
        score=row.score,
        status=row.status,
        heading_path=row.heading_path,
        authority=row.authority,
        last_reviewed=row.last_reviewed,
        has_prompt_injection=row.has_prompt_injection,
        has_sensitive_content=row.has_sensitive_content,
        chunk_metadata=row.chunk_metadata,
        chunk_index=row.chunk_index,
    )


def collect_warnings(
    chunks: list[RankedChunk],
    *,
    today: date,
    stale_after_days: int | None,
    weak_evidence_threshold: float | None = None,
    relevance_floor: float | None = None,
    max_warning_rank: int | None = None,
    isolated_match_drop_threshold: float | None = None,
) -> list[Warning]:
    """Concatenate the standard slice-2 warning set.

    ``relevance_floor`` / ``max_warning_rank`` propagate to the
    per-chunk security emitters only (#161); stale, deprecated, and
    weak-evidence are deliberately unaffected — they're either
    document-property warnings or operate on the best chunk overall.
    ``isolated_match_drop_threshold`` (#227) gates the top-1/top-2
    gap warning; passing ``None`` disables that emitter entirely.
    """
    warnings: list[Warning] = []
    warnings.extend(stale_warnings(chunks, today=today, stale_after_days=stale_after_days))
    warnings.extend(deprecated_warnings(chunks))
    warnings.extend(
        prompt_injection_warnings(
            chunks,
            relevance_floor=relevance_floor,
            max_warning_rank=max_warning_rank,
        )
    )
    warnings.extend(
        sensitive_content_warnings(
            chunks,
            relevance_floor=relevance_floor,
            max_warning_rank=max_warning_rank,
        )
    )
    warnings.extend(weak_evidence_warning(chunks, threshold=weak_evidence_threshold))
    warnings.extend(
        isolated_match_warning(
            chunks,
            drop_threshold=isolated_match_drop_threshold,
            weak_evidence_threshold=weak_evidence_threshold,
        )
    )
    return warnings


def conflict_warnings(conflicts: list[Conflict]) -> list[Warning]:
    """One ``conflicting_sources`` warning per detected conflict.

    Conflicts are dual-surfaced: as structured :class:`Conflict`
    entries on the response AND as warning entries. The structured
    list is canonical for the ``requires_human_review`` decision
    (see :func:`ranking.requires_human_review` — it inspects the
    ``conflicts`` argument, not the warnings argument); the warning
    is purely for agents that only consume the warnings channel and
    would otherwise miss conflict surfacing.

    #310 step 2: constructs a ConflictingSourcesWarning variant
    (carrying source_ids + topic — fields the flat shape loses)
    then downgrades to flat at the return. Wire-shape preserved;
    the typed variant is what step 3 (/v2/) ships on the wire.
    """
    out: list[Warning] = []
    for c in conflicts:
        variant = ConflictingSourcesWarning(
            type="conflicting_sources",
            message=f"{len(c.source_ids)} active sources address {c.topic!r}.",
            source_ids=list(c.source_ids),
            topic=c.topic,
        )
        out.append(downgrade_to_flat(variant))
    return out


def query_normalized_warning(removed_phrases: list[str]) -> Warning:
    """One ``query_normalized`` warning when the caller's query was sanitized (#100).

    Lists the phrase sources that matched so an operator auditing the
    response can spot a query attempting to exfiltrate prompt-
    injection content from the corpus. The list is informational —
    the cleaned query has already gone through retrieval.

    #310 step 2: constructs a QueryNormalizedWarning variant
    (carrying the typed removed_phrases list) then downgrades.
    """
    sample = ", ".join(repr(p) for p in removed_phrases[:3])
    suffix = f" (and {len(removed_phrases) - 3} more)" if len(removed_phrases) > 3 else ""
    variant = QueryNormalizedWarning(
        type="query_normalized",
        message=(
            f"Query contained prompt-injection markers; stripped before retrieval: "
            f"{sample}{suffix}."
        ),
        removed_phrases=list(removed_phrases),
    )
    return downgrade_to_flat(variant)


def document_refs_from_rows(rows: list[SearchRow]) -> dict[UUID, Any]:
    """Build ``{document_id: DocumentRef}`` from search rows.

    SearchRow carries the document-level fields the EvidenceChunk
    shape needs; collapse to one ref per document_id (later rows
    don't overwrite — same document, same metadata). ``DocumentRef``
    is lazy-imported here to avoid the retrieval ↔ agent cycle.

    ``source_url`` flows from ``documents.source_url`` through the
    CTE projection (#24); ingestion populates it from frontmatter
    ``source_url:`` for now. ``None`` is fine — the UI falls back to
    rendering the plain ``repo/path`` string.
    """
    from cf_knowledge_kiln.agent.serializers import DocumentRef

    refs: dict[UUID, Any] = {}
    for row in rows:
        if row.document_id in refs:
            continue
        refs[row.document_id] = DocumentRef(
            document_id=row.document_id,
            title=row.title,
            repo=row.repo,
            path=row.path,
            source_url=row.source_url,
            commit_sha=row.commit_sha,
            authority=row.authority,
            owner=row.owner,
        )
    return refs


def require_nonempty(query: str) -> None:
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")


__all__ = [
    "collect_warnings",
    "conflict_warnings",
    "document_refs_from_rows",
    "query_normalized_warning",
    "require_nonempty",
    "row_to_ranked_chunk",
]
