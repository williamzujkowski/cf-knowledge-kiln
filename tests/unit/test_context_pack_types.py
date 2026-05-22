"""Pydantic model contracts for the slice-3 context-pack shapes.

Drift vs openapi.yaml is enforced by test_openapi_drift.py once the
PHASE_5_ONLY_SCHEMAS tolerance is lifted. These tests cover
in-process model behavior: defaults, enums, constraints, extras
forbidden.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cf_knowledge_kiln.retrieval.types import (
    MAX_QUERY_LENGTH,
    MAX_TASK_LENGTH,
    ContextPackRequest,
    ContextPackResponse,
    EvidenceChunk,
    RelatedSource,
    TokenBudget,
)


class TestContextPackRequest:
    def test_defaults(self) -> None:
        req = ContextPackRequest(task="explain X", query="how does Y work")
        assert req.max_chunks == 8
        assert req.max_tokens == 3000
        assert req.include_summary is True
        assert req.require_citations is True
        assert req.filters is None

    def test_max_chunks_clamped_by_validator(self) -> None:
        with pytest.raises(ValidationError):
            ContextPackRequest(task="t", query="q", max_chunks=0)
        with pytest.raises(ValidationError):
            ContextPackRequest(task="t", query="q", max_chunks=51)

    def test_max_tokens_clamped_by_validator(self) -> None:
        with pytest.raises(ValidationError):
            ContextPackRequest(task="t", query="q", max_tokens=99)
        with pytest.raises(ValidationError):
            ContextPackRequest(task="t", query="q", max_tokens=32_001)

    def test_extras_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ContextPackRequest(task="t", query="q", surprise="oops")  # type: ignore[call-arg]

    def test_query_rejects_over_max_length(self) -> None:
        """A multi-MB query forces unbounded FTS + embedding compute."""
        with pytest.raises(ValidationError):
            ContextPackRequest(task="t", query="x" * (MAX_QUERY_LENGTH + 1))

    def test_task_rejects_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            ContextPackRequest(task="x" * (MAX_TASK_LENGTH + 1), query="q")

    def test_query_and_task_accept_at_max_length(self) -> None:
        req = ContextPackRequest(task="x" * MAX_TASK_LENGTH, query="y" * MAX_QUERY_LENGTH)
        assert len(req.task) == MAX_TASK_LENGTH
        assert len(req.query) == MAX_QUERY_LENGTH


class TestEvidenceChunk:
    def test_required_fields(self) -> None:
        c = EvidenceChunk(
            chunk_id=uuid4(),
            document_id=uuid4(),
            title="Doc",
            status="active",
            score=0.42,
            text="body",
        )
        assert c.repo is None
        assert c.path is None
        assert c.heading_path is None
        assert c.last_reviewed is None

    def test_carries_optional_metadata(self) -> None:
        c = EvidenceChunk(
            chunk_id=uuid4(),
            document_id=uuid4(),
            title="Runbook",
            repo="ops/playbooks",
            path="restart.md",
            heading_path=["Restart"],
            source_url="https://example.com/restart.md",  # type: ignore[arg-type]
            commit_sha="deadbeef",
            status="approved",
            authority="ops-team",
            owner="alice",
            last_reviewed=date(2026, 1, 1),
            score=1.23,
            text="step 1...",
        )
        assert c.commit_sha == "deadbeef"
        assert str(c.source_url).rstrip("/") == "https://example.com/restart.md"


class TestRelatedSource:
    def test_relationship_must_be_in_enum(self) -> None:
        with pytest.raises(ValidationError):
            RelatedSource(title="t", document_id=uuid4(), relationship="cousin_of")  # type: ignore[arg-type]

    def test_full_relationship_enum_accepted(self) -> None:
        for rel in ("supersedes", "superseded_by", "related_standard", "also_relevant"):
            r = RelatedSource(title="t", document_id=uuid4(), relationship=rel)  # type: ignore[arg-type]
            assert r.relationship == rel


class TestTokenBudget:
    def test_required_fields(self) -> None:
        b = TokenBudget(requested=3000, used_estimate=1450)
        assert b.requested == 3000
        assert b.used_estimate == 1450


class TestContextPackResponse:
    def test_minimal_required(self) -> None:
        pack = ContextPackResponse(
            context_pack_id=uuid4(),
            answerable=False,
            evidence=[],
            warnings=[],
            token_budget=TokenBudget(requested=3000, used_estimate=0),
            requires_human_review=True,
        )
        assert pack.evidence == []
        assert pack.warnings == []
        assert pack.conflicts == []
        assert pack.related_sources == []
        assert pack.review_reasons == []
        assert pack.untrusted_content_notice is None

    def test_evidence_and_warnings_are_required(self) -> None:
        """Hand-spec requires both; Pydantic must too (drift test enforces)."""
        with pytest.raises(ValidationError):
            ContextPackResponse(  # type: ignore[call-arg]
                context_pack_id=uuid4(),
                answerable=False,
                token_budget=TokenBudget(requested=100, used_estimate=0),
                requires_human_review=True,
            )

    def test_confidence_must_be_in_enum(self) -> None:
        with pytest.raises(ValidationError):
            ContextPackResponse(
                context_pack_id=uuid4(),
                answerable=True,
                evidence=[],
                warnings=[],
                confidence="meh",  # type: ignore[arg-type]
                token_budget=TokenBudget(requested=100, used_estimate=10),
                requires_human_review=False,
            )

    def test_extras_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ContextPackResponse(
                context_pack_id=uuid4(),
                answerable=True,
                evidence=[],
                warnings=[],
                token_budget=TokenBudget(requested=100, used_estimate=10),
                requires_human_review=False,
                surprise=1,  # type: ignore[call-arg]
            )
