"""Ranking primitives for hybrid retrieval (Phase 5).

Per ADR-0009: vector + FTS arms each return a top-K ranked list; this
module fuses them via Reciprocal Rank Fusion (RRF, k=60), applies
authority/status/freshness boosts, and reasons about the resulting
chunk set to emit warnings + conflicts + the ``requires_human_review``
decision.

Everything here is pure functions over plain inputs so unit tests
don't need a database. The DB-touching engine in
:mod:`cf_knowledge_kiln.retrieval.engine` (Phase 5 slice 2) calls
these to score candidates after the CTE returns.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import UUID

from cf_knowledge_kiln.retrieval.config import RetrievalConfig
from cf_knowledge_kiln.retrieval.types import Conflict, Warning

DEFAULT_RRF_K: int = 60
"""Standard RRF constant per the Cormack et al. paper; tuneable later."""

WEAK_EVIDENCE_SCORE_THRESHOLD: float = 0.015
"""A chunk below this fused+boosted score is considered weak evidence.

Module-level constant retained as the default; the value is also exposed
on :class:`RetrievalConfig.weak_evidence_score_threshold` so deployments
can tune it from ``config/security.yaml``. The threshold is calibrated
for the RRF k=60 fused score scale (top-1 in both arms is ≈ 0.0328);
under MockEmbeddingProvider scores collapse near zero and tests patch
this constant down to 1e-4 to keep the calibration signal meaningful.
"""


@dataclass(frozen=True)
class RankedChunk:
    """One ranked candidate. Engine populates from a SQL row.

    ``score`` is the post-fusion, post-boost number in roughly [0, 1].
    Tests construct these directly; the engine in slice 2 builds them
    from CTE rows.
    """

    chunk_id: UUID
    document_id: UUID
    score: float
    status: str
    heading_path: tuple[str, ...] = ()
    authority: str | None = None
    last_reviewed: date | None = None
    has_prompt_injection: bool = False
    has_sensitive_content: bool = False
    chunk_metadata: dict[str, object] = field(default_factory=dict)


# ─── RRF fusion ─────────────────────────────────────────────────────


def rrf_fuse(
    arms: list[list[UUID]],
    *,
    k: int = DEFAULT_RRF_K,
) -> dict[UUID, float]:
    """Fuse N ranked lists into a single score map per Reciprocal Rank Fusion.

    Each arm is a list of chunk_ids ordered best-first. For each chunk
    and each arm in which it appears at rank ``r`` (1-indexed), we add
    ``1 / (k + r)`` to its score. A chunk absent from an arm contributes
    nothing for that arm.

    Returns ``{chunk_id: fused_score}``. Empty input → empty output.
    """
    if not arms:
        return {}
    scores: dict[UUID, float] = defaultdict(float)
    for arm in arms:
        for rank, chunk_id in enumerate(arm, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    return dict(scores)


# ─── Boosts ─────────────────────────────────────────────────────────


def apply_boosts(
    chunks: list[RankedChunk],
    *,
    config: RetrievalConfig,
    today: date,
) -> list[RankedChunk]:
    """Return a copy of ``chunks`` with score multiplied by per-chunk boosts.

    Each chunk's score is ``base * status_weight * freshness_factor``.
    Ordering is preserved from the input; callers re-sort if needed.
    """
    return [_apply_boost_to_chunk(c, config=config, today=today) for c in chunks]


def _apply_boost_to_chunk(
    chunk: RankedChunk,
    *,
    config: RetrievalConfig,
    today: date,
) -> RankedChunk:
    status_w = config.weight_for_status(chunk.status)
    freshness = _freshness_factor(chunk.last_reviewed, today, config.stale_after_days)
    return RankedChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        score=chunk.score * status_w * freshness,
        status=chunk.status,
        heading_path=chunk.heading_path,
        authority=chunk.authority,
        last_reviewed=chunk.last_reviewed,
        has_prompt_injection=chunk.has_prompt_injection,
        has_sensitive_content=chunk.has_sensitive_content,
        chunk_metadata=chunk.chunk_metadata,
    )


def _freshness_factor(
    last_reviewed: date | None,
    today: date,
    stale_after_days: int | None,
) -> float:
    """Linear-decay freshness factor in [0.3, 1.0].

    Inside the stale window → 1.0. Past it, drop by 0.7 over the same
    number of days (so a doc 2 * stale_after_days old gets 0.3). Floor
    is 0.3 so even very old docs aren't completely erased — they may
    still be the only signal.

    ``last_reviewed = None`` is treated as stale (we don't know when
    it was reviewed). ``stale_after_days = None`` disables the check.
    """
    if stale_after_days is None:
        return 1.0
    if last_reviewed is None:
        return 0.3
    age = (today - last_reviewed).days
    if age <= stale_after_days:
        return 1.0
    decay = min(1.0, (age - stale_after_days) / stale_after_days)
    return max(0.3, 1.0 - 0.7 * decay)


# ─── Warnings ───────────────────────────────────────────────────────


def stale_warnings(
    chunks: list[RankedChunk],
    *,
    today: date,
    stale_after_days: int | None,
) -> list[Warning]:
    """Emit one ``stale_source`` per distinct doc with last_reviewed too old."""
    if stale_after_days is None:
        return []
    seen: set[UUID] = set()
    out: list[Warning] = []
    threshold = today - timedelta(days=stale_after_days)
    for c in chunks:
        if c.document_id in seen:
            continue
        if c.last_reviewed is None or c.last_reviewed < threshold:
            seen.add(c.document_id)
            out.append(
                Warning(
                    type="stale_source",
                    message=(
                        f"Document last reviewed {c.last_reviewed or 'never'}; "
                        f"older than {stale_after_days} days."
                    ),
                    source_id=c.document_id,
                )
            )
    return out


def deprecated_warnings(chunks: list[RankedChunk]) -> list[Warning]:
    """One ``deprecated_source`` warning per distinct deprecated/archived/superseded doc."""
    bad = {"deprecated", "archived", "superseded"}
    seen: set[UUID] = set()
    out: list[Warning] = []
    for c in chunks:
        if c.status in bad and c.document_id not in seen:
            seen.add(c.document_id)
            out.append(
                Warning(
                    type="deprecated_source",
                    message=f"Document status is {c.status!r}.",
                    source_id=c.document_id,
                )
            )
    return out


def prompt_injection_warnings(
    chunks: list[RankedChunk],
    *,
    relevance_floor: float | None = None,
    max_warning_rank: int | None = None,
) -> list[Warning]:
    """One ``prompt_injection_pattern`` per chunk flagged at ingest.

    Set by :mod:`cf_knowledge_kiln.ingestion.prompt_injection` when a
    chunk matched a phrase from ``config/security.yaml``; retrieval
    surfaces the warning only for chunks that cleared the rank +
    score gates (#161). See :func:`_gate_for_warnings`.
    """
    eligible = _gate_for_warnings(
        chunks, relevance_floor=relevance_floor, max_warning_rank=max_warning_rank
    )
    return [
        Warning(
            type="prompt_injection_pattern",
            message="Chunk contains a configured prompt-injection phrase.",
            source_id=c.document_id,
        )
        for c in eligible
        if c.has_prompt_injection
    ]


def sensitive_content_warnings(
    chunks: list[RankedChunk],
    *,
    relevance_floor: float | None = None,
    max_warning_rank: int | None = None,
) -> list[Warning]:
    """One ``sensitive_content`` per chunk flagged at ingest (#100).

    Set by :mod:`cf_knowledge_kiln.ingestion.sensitive_content` when a
    chunk matched a regex from ``content_filters.sensitive_patterns``.
    The agent serializer drops these chunks from the context-pack body
    entirely; humans see them with the warning attached. Emission is
    rank + score gated (#161); see :func:`_gate_for_warnings`.
    """
    eligible = _gate_for_warnings(
        chunks, relevance_floor=relevance_floor, max_warning_rank=max_warning_rank
    )
    return [
        Warning(
            type="sensitive_content",
            message="Chunk matches a configured sensitive-content pattern.",
            source_id=c.document_id,
        )
        for c in eligible
        if c.has_sensitive_content
    ]


def _gate_for_warnings(
    chunks: list[RankedChunk],
    *,
    relevance_floor: float | None,
    max_warning_rank: int | None,
) -> list[RankedChunk]:
    """Pre-filter ``chunks`` for the relevance-aware warning emitters (#161).

    Returns the prefix that satisfies BOTH ``rank ≤ max_warning_rank``
    (1-indexed; ``None`` disables) AND ``score ≥ relevance_floor``
    (``None`` disables). The per-chunk score check (rather than
    short-circuiting on the first failure) keeps the function correct
    under future input shapes where a caller hasn't sorted by score.
    """
    if max_warning_rank is None and relevance_floor is None:
        return list(chunks)
    rank_limit = len(chunks) if max_warning_rank is None else max_warning_rank
    floor = float("-inf") if relevance_floor is None else relevance_floor
    return [c for c in chunks[:rank_limit] if c.score >= floor]


def weak_evidence_warning(
    chunks: list[RankedChunk], *, threshold: float | None = None
) -> list[Warning]:
    """One ``weak_evidence`` warning if no chunk meets the score threshold.

    ``threshold`` overrides the module-level
    :data:`WEAK_EVIDENCE_SCORE_THRESHOLD` for this call — the engine
    passes ``RetrievalConfig.weak_evidence_score_threshold`` so a
    YAML-configured value actually fires. ``None`` means "use the
    module default" (preserves the old single-arg call shape for
    existing callers/tests).
    """
    effective = WEAK_EVIDENCE_SCORE_THRESHOLD if threshold is None else threshold
    if not chunks:
        return [Warning(type="weak_evidence", message="No matching evidence found.")]
    best = max(c.score for c in chunks)
    if best < effective:
        return [
            Warning(
                type="weak_evidence",
                message=f"Best chunk score {best:.2f} below threshold {effective}.",
            )
        ]
    return []


# ─── Conflict detection ─────────────────────────────────────────────


def detect_conflicts(
    chunks: list[RankedChunk],
    *,
    relevance_floor: float | None = None,
    max_warning_rank: int | None = None,
) -> list[Conflict]:
    """Phase 5 first cut: syntactic conflict on shared heading_path.

    Two or more distinct documents that both return a chunk under the
    same heading_path are flagged as ``conflicting_sources``. The
    Phase 5 design doc explicitly defers semantic ("doc A says X, doc
    B says not-X") conflict detection — that requires an LLM mediator.

    Active-only: deprecated/archived/superseded docs don't compete.
    Heading paths shorter than 1 element don't participate.
    Pre-filtered with :func:`_gate_for_warnings` (#161): each chunk
    in a candidate pair must clear ``relevance_floor`` and rank ≤
    ``max_warning_rank``; ``None`` on either disables that gate.
    """
    gated = _gate_for_warnings(
        chunks, relevance_floor=relevance_floor, max_warning_rank=max_warning_rank
    )
    bad = {"deprecated", "archived", "superseded"}
    by_path: dict[tuple[str, ...], set[UUID]] = defaultdict(set)
    for c in gated:
        if c.status in bad or not c.heading_path:
            continue
        by_path[c.heading_path].add(c.document_id)
    out: list[Conflict] = []
    for path, doc_ids in sorted(by_path.items()):
        if len(doc_ids) < 2:
            continue
        out.append(
            Conflict(
                topic=".".join(path),
                source_ids=sorted(doc_ids, key=str),
                description=(f"{len(doc_ids)} active sources address the same heading."),
            )
        )
    return out


# ─── requires_human_review ──────────────────────────────────────────


def requires_human_review(
    evidence: list[RankedChunk],
    warnings: list[Warning],
    conflicts: list[Conflict],
    *,
    weak_evidence_threshold: float | None = None,
) -> bool:
    """Single canonical decision per the Phase 5 design doc rules.

    Returns True iff ANY of:
    1. Any conflict was detected.
    2. The result set is empty.
    3. Every retrieved chunk is deprecated/archived/superseded.
    4. Every retrieved chunk is draft.
    5. Any warning has a type in {prompt_injection_pattern, sensitive_content}.
    6. The top-scoring chunk is below the weak-evidence threshold.

    ``weak_evidence_threshold`` overrides
    :data:`WEAK_EVIDENCE_SCORE_THRESHOLD` for this call — the agent
    serializer passes ``RetrievalConfig.weak_evidence_score_threshold``
    so a YAML override actually fires. ``None`` falls back to the
    module default.
    """
    if conflicts:
        return True
    if not evidence:
        return True
    statuses = {c.status for c in evidence}
    if statuses and statuses.issubset({"deprecated", "archived", "superseded"}):
        return True
    if statuses == {"draft"}:
        return True
    bad_types = {"prompt_injection_pattern", "sensitive_content"}
    if any(w.type in bad_types for w in warnings):
        return True
    effective = (
        WEAK_EVIDENCE_SCORE_THRESHOLD
        if weak_evidence_threshold is None
        else weak_evidence_threshold
    )
    return max(c.score for c in evidence) < effective


__all__ = [
    "DEFAULT_RRF_K",
    "WEAK_EVIDENCE_SCORE_THRESHOLD",
    "RankedChunk",
    "apply_boosts",
    "deprecated_warnings",
    "detect_conflicts",
    "prompt_injection_warnings",
    "requires_human_review",
    "rrf_fuse",
    "sensitive_content_warnings",
    "stale_warnings",
    "weak_evidence_warning",
]
