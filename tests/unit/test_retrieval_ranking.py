"""Unit tests for ranking primitives (Phase 5 slice 1)."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest

from cf_knowledge_kiln.retrieval import (
    DEFAULT_RRF_K,
    WEAK_EVIDENCE_SCORE_THRESHOLD,
    Conflict,
    RankedChunk,
    RetrievalConfig,
    Warning,
    apply_boosts,
    deprecated_warnings,
    detect_conflicts,
    prompt_injection_warnings,
    requires_human_review,
    rrf_fuse,
    stale_warnings,
    weak_evidence_warning,
)
from cf_knowledge_kiln.retrieval.ranking import sensitive_content_warnings

TODAY = date(2026, 5, 17)


def _mk(
    *,
    chunk_id: UUID | None = None,
    document_id: UUID | None = None,
    score: float = 1.0,
    status: str = "active",
    heading_path: tuple[str, ...] = (),
    authority: str | None = None,
    last_reviewed: date | None = TODAY,
    has_prompt_injection: bool = False,
    has_sensitive_content: bool = False,
) -> RankedChunk:
    return RankedChunk(
        chunk_id=chunk_id or uuid4(),
        document_id=document_id or uuid4(),
        score=score,
        status=status,
        heading_path=heading_path,
        authority=authority,
        last_reviewed=last_reviewed,
        has_prompt_injection=has_prompt_injection,
        has_sensitive_content=has_sensitive_content,
    )


# ─── RRF fusion ─────────────────────────────────────────────────────


class TestRrfFuse:
    def test_empty_input_returns_empty(self) -> None:
        assert rrf_fuse([]) == {}

    def test_single_arm_assigns_descending_scores(self) -> None:
        a, b, c = uuid4(), uuid4(), uuid4()
        scores = rrf_fuse([[a, b, c]])
        assert scores[a] > scores[b] > scores[c]
        assert scores[a] == pytest.approx(1.0 / (DEFAULT_RRF_K + 1))
        assert scores[c] == pytest.approx(1.0 / (DEFAULT_RRF_K + 3))

    def test_two_arms_sum_per_chunk(self) -> None:
        """A chunk appearing at rank 1 in both arms outranks one at rank 1 in just one."""
        a, b, c = uuid4(), uuid4(), uuid4()
        scores = rrf_fuse([[a, b], [a, c]])
        assert scores[a] == pytest.approx(2 * (1.0 / (DEFAULT_RRF_K + 1)))
        assert scores[b] == pytest.approx(1.0 / (DEFAULT_RRF_K + 2))
        assert scores[c] == pytest.approx(1.0 / (DEFAULT_RRF_K + 2))
        assert scores[a] > scores[b]

    def test_k_param_changes_curve_slope(self) -> None:
        """Larger k flattens the rank-vs-score curve (lower scores overall)."""
        a, b = uuid4(), uuid4()
        small_k = rrf_fuse([[a, b]], k=10)
        large_k = rrf_fuse([[a, b]], k=1000)
        assert small_k[a] > large_k[a]
        # Ratio between rank-1 and rank-2 also flattens with larger k.
        assert (small_k[a] / small_k[b]) > (large_k[a] / large_k[b])


# ─── Boosts ─────────────────────────────────────────────────────────


class TestApplyBoosts:
    def test_active_today_keeps_score(self) -> None:
        config = RetrievalConfig()
        [chunk] = apply_boosts([_mk(score=0.8)], config=config, today=TODAY)
        assert chunk.score == pytest.approx(0.8)

    def test_deprecated_loses(self) -> None:
        config = RetrievalConfig()
        active = _mk(score=1.0, status="active")
        deprecated = _mk(score=1.0, status="deprecated")
        boosted = apply_boosts([active, deprecated], config=config, today=TODAY)
        assert boosted[0].score > boosted[1].score
        assert boosted[1].score == pytest.approx(0.2)  # status weight

    def test_stale_loses_freshness(self) -> None:
        config = RetrievalConfig(stale_after_days=30)
        fresh = _mk(score=1.0, last_reviewed=TODAY)
        old = _mk(score=1.0, last_reviewed=TODAY - timedelta(days=200))
        boosted = apply_boosts([fresh, old], config=config, today=TODAY)
        assert boosted[0].score > boosted[1].score
        # 200 days = 30 fresh + 170 over → big decay, but floor at 0.3.
        assert boosted[1].score == pytest.approx(0.3)

    def test_never_reviewed_treated_as_stale(self) -> None:
        config = RetrievalConfig(stale_after_days=30)
        [chunk] = apply_boosts([_mk(score=1.0, last_reviewed=None)], config=config, today=TODAY)
        assert chunk.score == pytest.approx(0.3)

    def test_stale_after_days_none_disables_freshness(self) -> None:
        config = RetrievalConfig(stale_after_days=None)
        [chunk] = apply_boosts([_mk(score=1.0, last_reviewed=None)], config=config, today=TODAY)
        assert chunk.score == pytest.approx(1.0)

    def test_unknown_status_weight_is_one(self) -> None:
        config = RetrievalConfig()
        [chunk] = apply_boosts([_mk(score=1.0, status="experimental")], config=config, today=TODAY)
        assert chunk.score == pytest.approx(1.0)

    def test_freshness_at_stale_boundary_keeps_full_score(self) -> None:
        """Age == stale_after_days is still inside the freshness window."""
        config = RetrievalConfig(stale_after_days=30)
        [chunk] = apply_boosts(
            [_mk(score=1.0, last_reviewed=TODAY - timedelta(days=30))],
            config=config,
            today=TODAY,
        )
        assert chunk.score == pytest.approx(1.0)

    def test_freshness_midpoint_is_between_full_and_floor(self) -> None:
        """At age = 1.5 * stale_after_days the factor should be 1.0 - 0.7 * 0.5 = 0.65."""
        config = RetrievalConfig(stale_after_days=30)
        [chunk] = apply_boosts(
            [_mk(score=1.0, last_reviewed=TODAY - timedelta(days=45))],
            config=config,
            today=TODAY,
        )
        assert chunk.score == pytest.approx(0.65)


# ─── Warnings ───────────────────────────────────────────────────────


class TestStaleWarnings:
    def test_emits_one_per_distinct_doc(self) -> None:
        doc_a = uuid4()
        doc_b = uuid4()
        chunks = [
            _mk(document_id=doc_a, last_reviewed=TODAY - timedelta(days=400)),
            _mk(document_id=doc_a, last_reviewed=TODAY - timedelta(days=400)),
            _mk(document_id=doc_b, last_reviewed=TODAY - timedelta(days=10)),
        ]
        warnings = stale_warnings(chunks, today=TODAY, stale_after_days=365)
        assert len(warnings) == 1
        assert warnings[0].source_id == doc_a
        assert warnings[0].type == "stale_source"

    def test_never_reviewed_is_stale(self) -> None:
        doc = uuid4()
        warnings = stale_warnings(
            [_mk(document_id=doc, last_reviewed=None)],
            today=TODAY,
            stale_after_days=365,
        )
        assert len(warnings) == 1
        assert warnings[0].source_id == doc

    def test_none_threshold_emits_nothing(self) -> None:
        chunks = [_mk(last_reviewed=None)]
        assert stale_warnings(chunks, today=TODAY, stale_after_days=None) == []


class TestDeprecatedWarnings:
    @pytest.mark.parametrize("status", ["deprecated", "archived", "superseded"])
    def test_emits_one_per_bad_status_doc(self, status: str) -> None:
        doc = uuid4()
        warnings = deprecated_warnings([_mk(document_id=doc, status=status)])
        assert len(warnings) == 1
        assert warnings[0].source_id == doc

    def test_active_doc_emits_nothing(self) -> None:
        assert deprecated_warnings([_mk(status="active")]) == []

    def test_dedups_by_document_id(self) -> None:
        doc = uuid4()
        warnings = deprecated_warnings(
            [
                _mk(document_id=doc, status="deprecated"),
                _mk(document_id=doc, status="deprecated"),
            ]
        )
        assert len(warnings) == 1


class TestPromptInjectionWarnings:
    def test_emits_one_per_flagged_chunk(self) -> None:
        a, b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=a, has_prompt_injection=True),
            _mk(document_id=b, has_prompt_injection=False),
            _mk(document_id=a, has_prompt_injection=True),
        ]
        warnings = prompt_injection_warnings(chunks)
        assert len(warnings) == 2
        assert all(w.type == "prompt_injection_pattern" for w in warnings)

    def test_rank_gate_drops_low_ranked_chunk(self) -> None:
        """#161: a flagged chunk past max_warning_rank is dropped.

        Rank 7 with a high score does NOT trip when max_warning_rank=3,
        because the cosine-noise case lives in the tail of top-K.
        """
        chunks = [
            _mk(score=0.5, has_prompt_injection=False),  # 1
            _mk(score=0.4, has_prompt_injection=False),  # 2
            _mk(score=0.3, has_prompt_injection=False),  # 3
            _mk(score=0.2, has_prompt_injection=False),  # 4
            _mk(score=0.15, has_prompt_injection=False),  # 5
            _mk(score=0.1, has_prompt_injection=False),  # 6
            _mk(score=0.05, has_prompt_injection=True),  # 7 — flagged, gated out
        ]
        warnings = prompt_injection_warnings(chunks, max_warning_rank=3)
        assert warnings == []

    def test_rank_1_flagged_chunk_trips_under_gate(self) -> None:
        """#161: rank-1 flagged chunk still trips. Security guarantee preserved."""
        chunks = [
            _mk(score=0.5, has_prompt_injection=True),
            _mk(score=0.4),
            _mk(score=0.3),
        ]
        [warning] = prompt_injection_warnings(chunks, max_warning_rank=3, relevance_floor=0.015)
        assert warning.type == "prompt_injection_pattern"

    def test_relevance_floor_drops_low_score_flagged_chunk(self) -> None:
        """#161: a flagged chunk below the relevance floor is dropped even at rank 1."""
        chunks = [_mk(score=0.005, has_prompt_injection=True)]
        assert prompt_injection_warnings(chunks, relevance_floor=0.015) == []


class TestSensitiveContentWarnings:
    """Mirrors TestPromptInjectionWarnings — the gates are shared (#161)."""

    def test_emits_one_per_flagged_chunk(self) -> None:
        a, b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=a, has_sensitive_content=True),
            _mk(document_id=b, has_sensitive_content=False),
        ]
        [warning] = sensitive_content_warnings(chunks)
        assert warning.type == "sensitive_content"
        assert warning.source_id == a

    def test_rank_gate_drops_low_ranked_chunk(self) -> None:
        """#161: flagged chunk at rank 7 with score 0.5 does NOT trip when max_warning_rank=3."""
        chunks = [
            _mk(score=0.5),  # 1
            _mk(score=0.4),  # 2
            _mk(score=0.3),  # 3
            _mk(score=0.2),  # 4
            _mk(score=0.15),  # 5
            _mk(score=0.1),  # 6
            _mk(score=0.5, has_sensitive_content=True),  # 7 — flagged, gated by rank
        ]
        warnings = sensitive_content_warnings(chunks, max_warning_rank=3)
        assert warnings == []

    def test_rank_1_flagged_chunk_trips_under_gate(self) -> None:
        """#161: flagged chunk at rank 1 with score 0.5 DOES trip."""
        chunks = [
            _mk(score=0.5, has_sensitive_content=True),
            _mk(score=0.4),
            _mk(score=0.3),
        ]
        [warning] = sensitive_content_warnings(chunks, max_warning_rank=3, relevance_floor=0.015)
        assert warning.type == "sensitive_content"

    def test_relevance_floor_drops_low_score_flagged_chunk(self) -> None:
        """#161: a flagged chunk at rank 1 BELOW the relevance floor does not trip."""
        chunks = [_mk(score=0.005, has_sensitive_content=True)]
        assert sensitive_content_warnings(chunks, relevance_floor=0.015) == []

    def test_none_gates_preserve_legacy_behavior(self) -> None:
        """Both gates default to None → every flagged chunk in input trips."""
        a, b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=a, score=0.5, has_sensitive_content=True),
            _mk(document_id=b, score=0.001, has_sensitive_content=True),
        ]
        # No gating arg → all flagged chunks emit (pre-#161 behavior).
        assert len(sensitive_content_warnings(chunks)) == 2


class TestWeakEvidenceWarning:
    def test_empty_chunks_emits_no_matching_evidence(self) -> None:
        [w] = weak_evidence_warning([])
        assert w.type == "weak_evidence"
        assert "No matching" in w.message

    def test_below_threshold_emits_warning(self) -> None:
        # RRF-scale score — see WEAK_EVIDENCE_SCORE_THRESHOLD docstring.
        [w] = weak_evidence_warning([_mk(score=0.005)])
        assert w.type == "weak_evidence"

    def test_above_threshold_emits_nothing(self) -> None:
        assert weak_evidence_warning([_mk(score=WEAK_EVIDENCE_SCORE_THRESHOLD + 0.01)]) == []


# ─── Conflict detection ─────────────────────────────────────────────


class TestDetectConflicts:
    def test_same_heading_different_docs_is_conflict(self) -> None:
        doc_a, doc_b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=doc_a, heading_path=("Deployment", "Web")),
            _mk(document_id=doc_b, heading_path=("Deployment", "Web")),
        ]
        [conflict] = detect_conflicts(chunks)
        assert conflict.topic == "Deployment.Web"
        assert set(conflict.source_ids) == {doc_a, doc_b}

    def test_same_doc_no_conflict(self) -> None:
        doc = uuid4()
        chunks = [
            _mk(document_id=doc, heading_path=("X",)),
            _mk(document_id=doc, heading_path=("X",)),
        ]
        assert detect_conflicts(chunks) == []

    def test_deprecated_docs_dont_compete(self) -> None:
        doc_a, doc_b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=doc_a, heading_path=("X",), status="deprecated"),
            _mk(document_id=doc_b, heading_path=("X",), status="active"),
        ]
        assert detect_conflicts(chunks) == []

    def test_empty_heading_path_skipped(self) -> None:
        doc_a, doc_b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=doc_a, heading_path=()),
            _mk(document_id=doc_b, heading_path=()),
        ]
        assert detect_conflicts(chunks) == []

    def test_three_docs_same_heading_one_conflict_three_sources(self) -> None:
        a, b, c = uuid4(), uuid4(), uuid4()
        chunks = [
            _mk(document_id=a, heading_path=("X",)),
            _mk(document_id=b, heading_path=("X",)),
            _mk(document_id=c, heading_path=("X",)),
        ]
        [conflict] = detect_conflicts(chunks)
        assert len(conflict.source_ids) == 3

    def test_rank_1_and_2_above_floor_conflict_pair_trips(self) -> None:
        """#161: rank 1 + rank 2 with shared heading + both above floor → conflict."""
        a, b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=a, score=0.5, heading_path=("X",)),
            _mk(document_id=b, score=0.4, heading_path=("X",)),
            _mk(score=0.3),
            _mk(score=0.2),
        ]
        [conflict] = detect_conflicts(chunks, max_warning_rank=3, relevance_floor=0.015)
        assert set(conflict.source_ids) == {a, b}

    def test_rank_5_and_6_pair_does_not_trip_under_rank_gate(self) -> None:
        """#161: same conflict pair at rank 5 + 6 → gated out by max_warning_rank=3."""
        a, b = uuid4(), uuid4()
        chunks = [
            _mk(score=0.5),  # 1
            _mk(score=0.4),  # 2
            _mk(score=0.3),  # 3
            _mk(score=0.2),  # 4
            _mk(document_id=a, score=0.15, heading_path=("X",)),  # 5
            _mk(document_id=b, score=0.1, heading_path=("X",)),  # 6
        ]
        assert detect_conflicts(chunks, max_warning_rank=3, relevance_floor=0.015) == []

    def test_relevance_floor_drops_one_side_of_pair(self) -> None:
        """#161: if only ONE chunk in the pair clears the floor, no conflict.

        Conflict needs ≥ 2 distinct doc_ids on the same heading_path.
        Dropping one side of the pair on the relevance gate leaves just
        one — same shape as the existing single-doc-no-conflict case.
        """
        a, b = uuid4(), uuid4()
        chunks = [
            _mk(document_id=a, score=0.5, heading_path=("X",)),
            _mk(document_id=b, score=0.005, heading_path=("X",)),  # below floor
        ]
        assert detect_conflicts(chunks, max_warning_rank=3, relevance_floor=0.015) == []

    def test_none_gates_preserve_legacy_behavior(self) -> None:
        """No gating args → conflict regardless of rank or score (pre-#161)."""
        a, b = uuid4(), uuid4()
        chunks = [
            _mk(score=0.5),
            _mk(score=0.4),
            _mk(score=0.3),
            _mk(score=0.2),
            _mk(document_id=a, score=0.005, heading_path=("X",)),  # below floor
            _mk(document_id=b, score=0.003, heading_path=("X",)),  # below floor
        ]
        # Without gating, both flagged chunks participate.
        [conflict] = detect_conflicts(chunks)
        assert set(conflict.source_ids) == {a, b}


# ─── requires_human_review ──────────────────────────────────────────


class TestRequiresHumanReview:
    def test_empty_evidence_requires_review(self) -> None:
        assert requires_human_review([], [], []) is True

    def test_strong_active_evidence_does_not(self) -> None:
        assert requires_human_review([_mk(score=0.9, status="active")], [], []) is False

    def test_any_conflict_requires_review(self) -> None:
        c = Conflict(topic="X", source_ids=[uuid4(), uuid4()])
        assert requires_human_review([_mk(score=0.9)], [], [c]) is True

    def test_all_deprecated_requires_review(self) -> None:
        chunks = [_mk(score=0.9, status="deprecated"), _mk(score=0.9, status="archived")]
        assert requires_human_review(chunks, [], []) is True

    def test_all_draft_requires_review(self) -> None:
        chunks = [_mk(score=0.9, status="draft"), _mk(score=0.9, status="draft")]
        assert requires_human_review(chunks, [], []) is True

    def test_prompt_injection_warning_requires_review(self) -> None:
        w = Warning(type="prompt_injection_pattern", message="x")
        assert requires_human_review([_mk(score=0.9)], [w], []) is True

    def test_sensitive_content_warning_requires_review(self) -> None:
        w = Warning(type="sensitive_content", message="x")
        assert requires_human_review([_mk(score=0.9)], [w], []) is True

    def test_weak_evidence_requires_review(self) -> None:
        # RRF-scale score — well below the 0.015 default threshold.
        chunks = [_mk(score=0.005)]
        assert requires_human_review(chunks, [], []) is True

    def test_stale_warning_alone_does_not_require_review(self) -> None:
        """Stale + deprecated warnings should NOT force human review by themselves."""
        w = Warning(type="stale_source", message="x")
        assert requires_human_review([_mk(score=0.9, status="active")], [w], []) is False
