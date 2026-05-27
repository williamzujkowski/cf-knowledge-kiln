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
from cf_knowledge_kiln.retrieval.warning_variants import (
    DeprecatedSourceWarning,
    IsolatedMatchWarning,
    PromptInjectionPatternWarning,
    SensitiveContentWarning,
    StaleSourceWarning,
    WeakEvidenceWarning,
    downgrade_to_flat,
)

DEFAULT_RRF_K: int = 60
"""Standard RRF constant per the Cormack et al. paper; tuneable later."""

WEAK_EVIDENCE_SCORE_THRESHOLD: float = 0.46
"""A chunk below this fused+boosted score is considered weak evidence.

Module-level constant retained as the default; the value is also exposed
on :class:`RetrievalConfig.weak_evidence_score_threshold` so deployments
can tune it from ``config/security.yaml``. The threshold is calibrated
for the normalized fused score (#164): the SQL emits
``SUM(1/(k+rnk)) * (k+1)/2`` so a both-arm rank-1 hit normalizes to
``1.0`` and a single-arm rank-1 hit to ``0.5``. ``0.46`` is the
proportional re-baseline of the pre-#164 ``0.015`` raw-scale threshold
(``0.015 * 30.5 ~= 0.46``), so the same fraction of chunks trip
weak-evidence as before but the constant is interpretable as "below
~92% of single-arm rank-1." Under MockEmbeddingProvider scores still
collapse near zero and tests patch this constant down to 1e-4 to keep
the calibration signal meaningful.
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
    # #337: 0-based section index within the document. Flows from
    # SearchRow → ResultCard so the result card can render
    # "section N" without re-querying the chunks table. Defaults to
    # 0 for synthetic ranked chunks (tests, mocks) that don't carry
    # a real index — same default as DocumentChunk's column would
    # apply for a freshly-inserted row at section position 1.
    chunk_index: int = 0
    # #384: total section count for the parent document so the UI
    # can render "section N of M". Defaults to 0 for synthetic
    # ranked chunks — the template treats 0 as "unknown" and falls
    # back to bare "section N" rendering.
    chunk_count: int = 0


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
        chunk_index=chunk.chunk_index,
        # #384: preserve through boost reconstruction.
        chunk_count=chunk.chunk_count,
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
    """Emit one ``stale_source`` per distinct doc with last_reviewed too old.

    #310 step 2: constructs StaleSourceWarning variants (carrying
    stale_after_days threshold + last_reviewed) then downgrades.
    """
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
            variant = StaleSourceWarning(
                type="stale_source",
                message=(
                    f"Document last reviewed {c.last_reviewed or 'never'}; "
                    f"older than {stale_after_days} days."
                ),
                source_id=c.document_id,
                stale_after_days=stale_after_days,
                last_reviewed=c.last_reviewed,
            )
            out.append(downgrade_to_flat(variant))
    return out


def deprecated_warnings(chunks: list[RankedChunk]) -> list[Warning]:
    """One ``deprecated_source`` warning per distinct deprecated/archived/superseded doc.

    #310 step 2: constructs DeprecatedSourceWarning variants
    (carrying the specific document_status) then downgrades.
    """
    bad: set[str] = {"deprecated", "archived", "superseded"}
    seen: set[UUID] = set()
    out: list[Warning] = []
    for c in chunks:
        if c.status in bad and c.document_id not in seen:
            seen.add(c.document_id)
            # Cast c.status into the variant's narrower Literal type;
            # the bad-set guard above ensures it's one of three values.
            variant = DeprecatedSourceWarning(
                type="deprecated_source",
                message=f"Document status is {c.status!r}.",
                source_id=c.document_id,
                document_status=c.status,  # type: ignore[arg-type]
            )
            out.append(downgrade_to_flat(variant))
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

    #310 step 2: constructs PromptInjectionPatternWarning variants
    (carrying chunk_id — brand-new info the flat shape never had —
    plus the existing source_id) then downgrades. ``pattern_id``
    stays None pending ingest-side widening.
    """
    eligible = _gate_for_warnings(
        chunks, relevance_floor=relevance_floor, max_warning_rank=max_warning_rank
    )
    out: list[Warning] = []
    for c in eligible:
        if not c.has_prompt_injection:
            continue
        variant = PromptInjectionPatternWarning(
            type="prompt_injection_pattern",
            message="Chunk contains a configured prompt-injection phrase.",
            source_id=c.document_id,
            chunk_id=c.chunk_id,
        )
        out.append(downgrade_to_flat(variant))
    return out


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

    #310 step 2: constructs SensitiveContentWarning variants (now
    carrying chunk_id alongside source_id) then downgrades.
    ``classifier_label`` stays None pending ingest-side widening.
    """
    eligible = _gate_for_warnings(
        chunks, relevance_floor=relevance_floor, max_warning_rank=max_warning_rank
    )
    out: list[Warning] = []
    for c in eligible:
        if not c.has_sensitive_content:
            continue
        variant = SensitiveContentWarning(
            type="sensitive_content",
            message="Chunk matches a configured sensitive-content pattern.",
            source_id=c.document_id,
            chunk_id=c.chunk_id,
        )
        out.append(downgrade_to_flat(variant))
    return out


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
        # #310 step 2: empty-input shape — top_score is None.
        variant = WeakEvidenceWarning(
            type="weak_evidence",
            message="No matching evidence found.",
            top_score=None,
            score_floor=effective,
        )
        return [downgrade_to_flat(variant)]
    best = max(c.score for c in chunks)
    if best < effective:
        # #310 step 2: below-floor shape — top_score is the best
        # observed score (still < score_floor).
        variant = WeakEvidenceWarning(
            type="weak_evidence",
            message=f"Best chunk score {best:.2f} below threshold {effective}.",
            top_score=best,
            score_floor=effective,
        )
        return [downgrade_to_flat(variant)]
    return []


def isolated_match_warning(
    chunks: list[RankedChunk],
    *,
    drop_threshold: float | None,
    weak_evidence_threshold: float | None = None,
) -> list[Warning]:
    """One ``isolated_match`` warning if the top chunk towers over the runner-up.

    Fires when:

    1. There are at least two candidates (a one-result list can't be
       "isolated" relative to peers; weak_evidence handles the empty /
       single-low case).
    2. The top chunk's score is **above** the weak-evidence threshold —
       i.e. the score *looks* like real signal. Below it, we let
       :func:`weak_evidence_warning` own the framing and avoid a
       confusing double-warning that says "both strong-but-isolated AND
       weak."
    3. The gap between the top-1 score and the top-2 score exceeds
       ``drop_threshold``.

    This pattern was the dominant cause of false-positive answers in
    the homelab-iac calibration eval (#222 → #227): queries about
    topics with one passing mention in the corpus (e.g. "configure
    Kubernetes" against a homelab that mentions Kubernetes once in
    cf-deployment notes) produced a top-1 score of ~0.9 with no other
    candidates near it. The high score alone reads as confident; the
    *shape* of the distribution is the actual tell.

    ``drop_threshold = None`` disables the gate (returns ``[]``). This
    is the off-switch for operators who'd rather tolerate the false
    positives than nudge real-answer queries into human review.
    """
    if drop_threshold is None:
        return []
    if len(chunks) < 2:
        return []
    weak_floor = (
        WEAK_EVIDENCE_SCORE_THRESHOLD
        if weak_evidence_threshold is None
        else weak_evidence_threshold
    )
    # #310 step 2 (item 1 of N): sort BY CHUNK (not just scalar score)
    # so we can attach source_id + chunk_id to the variant. Tie-break
    # by chunk_id for deterministic ordering when two chunks share the
    # same score — same convention as the rest of the engine.
    sorted_chunks = sorted(chunks, key=lambda c: (-c.score, str(c.chunk_id)))
    top_chunk, runner_up_chunk = sorted_chunks[0], sorted_chunks[1]
    top, runner_up = top_chunk.score, runner_up_chunk.score
    if top < weak_floor:
        return []
    gap = top - runner_up
    if gap <= drop_threshold:
        return []
    message = (
        f"Top result scored {top:.2f}, but the next best was only "
        f"{runner_up:.2f} (gap {gap:.2f} > {drop_threshold:.2f}). "
        "One chunk surface-matches the query without comparable "
        "supporting evidence."
    )
    # #310 step 2: construct the discriminated variant (carries
    # source_id + chunk_id + every per-variant scalar today's flat
    # shape loses) and downgrade to flat at the return so the
    # signature + wire contract are untouched. The variant is
    # captured by the engine-internal pipeline for the eventual /v2/
    # wire ship; until then, this is the proving ground that the
    # variants work against real engine data. See ADR-0011.
    variant = IsolatedMatchWarning(
        type="isolated_match",
        message=message,
        top_score=top,
        runner_up_score=runner_up,
        gap=gap,
        drop_threshold=drop_threshold,
        source_id=top_chunk.document_id,
        chunk_id=top_chunk.chunk_id,
    )
    return [downgrade_to_flat(variant)]


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
    5. Any warning has a type in
       {prompt_injection_pattern, sensitive_content, isolated_match}.
       ``isolated_match`` (#227) means top-1 dominates top-2 by more
       than the configured drop threshold — the "confidently wrong
       answer" shape from one corpus chunk surface-matching the query.
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
    bad_types = {"prompt_injection_pattern", "sensitive_content", "isolated_match"}
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
    "isolated_match_warning",
    "prompt_injection_warnings",
    "requires_human_review",
    "rrf_fuse",
    "sensitive_content_warnings",
    "stale_warnings",
    "weak_evidence_warning",
]
