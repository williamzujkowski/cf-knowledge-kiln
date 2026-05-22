"""Unit tests for the agent context-pack serializer (Phase 5 slice 3).

The serializer turns a list of post-boost RankedChunks (from the
HybridRetriever) into the agent-shaped ContextPackResponse, applying:

* token budgeting — chunks added until ``max_tokens`` is hit
* chunk count cap — never more than ``max_chunks`` evidence pieces
* the standard untrusted-content notice preamble (always present)
* related-source lookup from supersedes/superseded_by links
* confidence derivation from score + warnings
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from cf_knowledge_kiln.agent.serializers import (
    UNTRUSTED_CONTENT_NOTICE,
    DocumentRef,
    SerializerInputs,
    assemble_context_pack,
    derive_confidence,
    trim_evidence_to_budget,
)
from cf_knowledge_kiln.retrieval.ranking import RankedChunk
from cf_knowledge_kiln.retrieval.types import Conflict, Warning


def _chunk(
    *,
    score: float = 0.9,
    status: str = "active",
    text: str = "x" * 100,
    heading: tuple[str, ...] = ("Heading",),
    document_id=None,
    last_reviewed: date | None = None,
    chunk_metadata=None,
    has_sensitive_content: bool = False,
) -> RankedChunk:
    return RankedChunk(
        chunk_id=uuid4(),
        document_id=document_id or uuid4(),
        score=score,
        status=status,
        heading_path=heading,
        last_reviewed=last_reviewed,
        has_sensitive_content=has_sensitive_content,
        chunk_metadata=chunk_metadata or {"text": text},
    )


class TestTrimEvidenceToBudget:
    def test_returns_all_when_budget_exceeds_total(self) -> None:
        chunks = [_chunk() for _ in range(3)]
        contents = ["short text"] * 3
        kept, used = trim_evidence_to_budget(
            chunks, contents=contents, max_chunks=10, max_tokens=10_000
        )
        assert len(kept) == 3
        assert used > 0

    def test_stops_when_max_chunks_hit(self) -> None:
        chunks = [_chunk() for _ in range(10)]
        contents = ["short"] * 10
        kept, _ = trim_evidence_to_budget(
            chunks, contents=contents, max_chunks=4, max_tokens=10_000
        )
        assert len(kept) == 4

    def test_stops_when_token_budget_hit(self) -> None:
        chunks = [_chunk() for _ in range(20)]
        # Each ~100 chars ~ several tokens; budget 30 tokens should
        # only fit one or two chunks.
        contents = ["x " * 50] * 20
        kept, _ = trim_evidence_to_budget(chunks, contents=contents, max_chunks=20, max_tokens=30)
        assert 0 < len(kept) < 20

    def test_always_keeps_at_least_one_chunk_when_present(self) -> None:
        """A single huge chunk should still be returned, even past budget.

        Otherwise an agent gets an empty context for a query that has a
        clear best match. The notice text + warnings still surface.
        """
        chunks = [_chunk()]
        contents = ["x " * 10_000]  # blows the budget
        kept, _ = trim_evidence_to_budget(chunks, contents=contents, max_chunks=10, max_tokens=20)
        assert len(kept) == 1

    def test_empty_inputs_return_empty(self) -> None:
        kept, used = trim_evidence_to_budget([], contents=[], max_chunks=5, max_tokens=100)
        assert kept == []
        assert used == 0

    def test_raises_on_contents_chunks_length_mismatch(self) -> None:
        """#172: misaligned contents/chunks must fail loudly, not silently
        truncate — the two lists are zipped index-for-index."""
        chunks = [_chunk() for _ in range(3)]
        contents = ["only one"]
        with pytest.raises(ValueError, match="length mismatch"):
            trim_evidence_to_budget(chunks, contents=contents, max_chunks=5, max_tokens=100)


class TestDeriveConfidence:
    def test_empty_evidence_is_none(self) -> None:
        assert derive_confidence([], warnings=[]) == "none"

    def test_top_below_threshold_is_low(self) -> None:
        # Normalized score — below the 0.46 default weak-evidence floor.
        chunks = [_chunk(score=0.01)]
        assert derive_confidence(chunks, warnings=[]) == "low"

    def test_top_above_threshold_with_warning_drops_one_step(self) -> None:
        chunks = [_chunk(score=0.9)]
        w = [Warning(type="stale_source", message="old")]
        # High would become medium when a freshness/deprecation warning is present.
        assert derive_confidence(chunks, warnings=w) == "medium"

    def test_top_above_threshold_no_warnings_is_high(self) -> None:
        chunks = [_chunk(score=0.95)]
        assert derive_confidence(chunks, warnings=[]) == "high"

    def test_high_bucket_reachable_on_clean_top_hit(self) -> None:
        """#164 — the ``high`` cutoff fires on a normalized both-arm hit.

        Pre-normalization the RRF sum maxed out at ``2/(k+1) ≈ 0.0328``
        so the ``score >= 0.8`` cutoff in :func:`derive_confidence` was
        structurally unreachable and every clean hit collapsed to
        ``medium``. Post-normalization a clean both-arm top-1 lands at
        ``~1.0`` and a marginal both-arm hit at ``~0.85`` should clear
        the bar.
        """
        chunks = [_chunk(score=0.85)]
        assert derive_confidence(chunks, warnings=[]) == "high"

    def test_medium_band_between_floor_and_high(self) -> None:
        """Single-arm top-1 normalizes to ``~0.5`` — solidly ``medium``.

        Verifies the band ``[weak_evidence_floor, 0.8)`` resolves to
        ``medium`` rather than collapsing toward either edge.
        """
        chunks = [_chunk(score=0.5)]
        assert derive_confidence(chunks, warnings=[]) == "medium"


class TestAssembleContextPack:
    def test_includes_untrusted_content_notice(self) -> None:
        chunks = [_chunk(score=0.8)]
        inputs = SerializerInputs(
            chunks=chunks,
            warnings=[],
            conflicts=[],
            chunk_text={chunks[0].chunk_id: "body text"},
            document_refs={chunks[0].document_id: _ref()},
            related_sources=[],
        )
        pack = assemble_context_pack(
            inputs, task="explain", query="how", max_chunks=8, max_tokens=3000
        )
        assert pack.untrusted_content_notice == UNTRUSTED_CONTENT_NOTICE

    def test_answerable_false_on_empty_evidence(self) -> None:
        inputs = SerializerInputs(
            chunks=[],
            warnings=[],
            conflicts=[],
            chunk_text={},
            document_refs={},
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=3000)
        assert pack.answerable is False
        assert pack.requires_human_review is True

    def test_requires_human_review_on_conflict(self) -> None:
        a, b = uuid4(), uuid4()
        chunks = [_chunk(score=0.9, document_id=a), _chunk(score=0.9, document_id=b)]
        conflicts = [Conflict(topic="Topic", source_ids=[a, b])]
        inputs = SerializerInputs(
            chunks=chunks,
            warnings=[],
            conflicts=conflicts,
            chunk_text={c.chunk_id: "body" for c in chunks},
            document_refs={
                a: _ref(document_id=a),
                b: _ref(document_id=b),
            },
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=3000)
        assert pack.requires_human_review is True
        assert pack.conflicts and pack.conflicts[0].topic == "Topic"

    def test_review_reasons_populated_when_review_required(self) -> None:
        inputs = SerializerInputs(
            chunks=[],
            warnings=[],
            conflicts=[],
            chunk_text={},
            document_refs={},
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=3000)
        assert pack.requires_human_review is True
        assert pack.review_reasons, "empty evidence should produce a reason"

    def test_token_budget_records_request_and_estimate(self) -> None:
        chunks = [_chunk(score=0.8)]
        inputs = SerializerInputs(
            chunks=chunks,
            warnings=[],
            conflicts=[],
            chunk_text={chunks[0].chunk_id: "body text"},
            document_refs={chunks[0].document_id: _ref()},
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=2222)
        assert pack.token_budget.requested == 2222
        assert pack.token_budget.used_estimate > 0

    def test_evidence_carries_text_and_title(self) -> None:
        chunk = _chunk(score=0.8)
        ref = _ref(document_id=chunk.document_id, title="A Doc")
        inputs = SerializerInputs(
            chunks=[chunk],
            warnings=[],
            conflicts=[],
            chunk_text={chunk.chunk_id: "the actual body"},
            document_refs={chunk.document_id: ref},
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=3000)
        assert pack.evidence
        ev = pack.evidence[0]
        assert ev.text == "the actual body"
        assert ev.title == "A Doc"

    def test_token_estimate_counts_the_whole_pack_not_just_content(self) -> None:
        """#189: used_estimate covers the envelope + notice, not only chunk text.

        An empty-evidence pack has zero chunk content but still ships the
        untrusted-content notice + JSON envelope — the estimate must
        reflect that, so an agent budgets against an honest number.
        """
        from cf_knowledge_kiln.agent.serializers import UNTRUSTED_CONTENT_NOTICE
        from cf_knowledge_kiln.ingestion.tokens import count_tokens

        inputs = SerializerInputs(
            chunks=[],
            warnings=[],
            conflicts=[],
            chunk_text={},
            document_refs={},
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=3000)
        # Old behaviour reported 0 for an empty pack; the estimate now
        # includes at least the always-present untrusted-content notice.
        assert pack.token_budget.used_estimate >= count_tokens(UNTRUSTED_CONTENT_NOTICE)

    def test_missing_document_ref_forces_review(self) -> None:
        """#189: a chunk with no DocumentRef is effectively uncited — the
        pack must flag review rather than silently emit an uncited chunk."""
        chunk = _chunk(score=0.9)
        inputs = SerializerInputs(
            chunks=[chunk],
            warnings=[],
            conflicts=[],
            chunk_text={chunk.chunk_id: "body"},
            document_refs={},  # no ref for the chunk
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=3000)
        assert pack.requires_human_review is True
        assert any("missing source citations" in r for r in pack.review_reasons)

    def test_dropped_sensitive_chunk_named_in_review_reasons(self) -> None:
        """#189: sensitive chunks are stripped from evidence — say so, so the
        agent knows evidence was removed, not just that a warning fired."""
        sensitive = _chunk(score=0.9, has_sensitive_content=True)
        inputs = SerializerInputs(
            chunks=[sensitive],
            warnings=[],
            conflicts=[],
            chunk_text={sensitive.chunk_id: "body"},
            document_refs={sensitive.document_id: _ref(document_id=sensitive.document_id)},
            related_sources=[],
        )
        pack = assemble_context_pack(inputs, task="t", query="q", max_chunks=8, max_tokens=3000)
        assert pack.evidence == []  # the sensitive chunk was dropped
        assert any("sensitive content were dropped" in r for r in pack.review_reasons)


def _ref(*, document_id=None, title: str = "T") -> DocumentRef:
    return DocumentRef(
        document_id=document_id or uuid4(),
        title=title,
        repo="r",
        path="p.md",
        source_url=None,
        commit_sha=None,
        authority=None,
        owner=None,
    )
