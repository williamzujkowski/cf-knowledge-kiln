"""Pins the #360 untrusted-content notice pinned phrases.

Three short canonical phrases ride alongside the prose notice on
every ContextPackResponse + AnswerResponse. A consumer that
templates the prompt + truncates at N tokens MUST verify each
phrase appears in the rendered output; a phrase missing = the
contract has been weakened.

Tests pin two things:
1. Every phrase appears verbatim inside UNTRUSTED_CONTENT_NOTICE
   (so the prose and the pinned list can't drift)
2. The phrases ride through assemble_context_pack onto the
   response object

Doc test verifies docs/agent-integration-guide.md documents the
verification recipe.
"""

from __future__ import annotations

from pathlib import Path

from cf_knowledge_kiln.agent.serializers import (
    UNTRUSTED_CONTENT_NOTICE,
    UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES,
)


class TestPinnedPhrasesInPolicy:
    def test_three_phrases(self) -> None:
        """The audit recommends exactly three phrases so a single-
        truncation drop still leaves the contract recognizable. Pin
        the count so a future addition is a deliberate design call."""
        assert len(UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES) == 3

    def test_each_phrase_is_short(self) -> None:
        """≤ 30 chars / ≤ 5 words per audit guidance — short enough
        to fit inside common prompt-template line caps without being
        splitable across lines."""
        for phrase in UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES:
            assert len(phrase) <= 30, f"{phrase!r} is too long for the contract"
            assert len(phrase.split()) <= 5, f"{phrase!r} is too many words"

    def test_phrases_are_distinct(self) -> None:
        """Each phrase pins a different aspect of the contract — the
        noun ('source evidence'), the negation ('not treat'), the
        exception clause ('explicitly authorizes'). A duplicate
        weakens the multi-channel signal."""
        assert len(set(UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES)) == 3


class TestPinnedPhrasesInProse:
    """Every phrase MUST appear verbatim in the canonical prose so
    the contract and the pinned list can't drift. If the prose
    changes such that a phrase no longer appears, the phrase or the
    prose has to be updated together (and `untrusted_content_notice_id`
    bumped if the meaning changed)."""

    def test_each_phrase_appears_in_notice(self) -> None:
        for phrase in UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES:
            assert phrase in UNTRUSTED_CONTENT_NOTICE, (
                f"Canonical prose drifted from pinned list: {phrase!r} "
                f"is no longer in UNTRUSTED_CONTENT_NOTICE. Either restore "
                f"the prose or update the pinned list + bump the notice id."
            )


class TestResponseShape:
    """Pin that the new field ships on both response models with
    the canonical phrase list populated."""

    def test_context_pack_response_carries_pinned_phrases(self) -> None:
        """End-to-end through assemble_context_pack."""
        from datetime import date
        from uuid import uuid4

        from cf_knowledge_kiln.agent.serializers import (
            DocumentRef,
            SerializerInputs,
            assemble_context_pack,
        )
        from cf_knowledge_kiln.retrieval.ranking import RankedChunk

        chunk_id = uuid4()
        doc_id = uuid4()
        inputs = SerializerInputs(
            chunks=[
                RankedChunk(
                    chunk_id=chunk_id,
                    document_id=doc_id,
                    score=0.9,
                    status="active",
                    heading_path=(),
                    last_reviewed=date.today(),
                )
            ],
            warnings=[],
            conflicts=[],
            chunk_text={chunk_id: "hello"},
            document_refs={doc_id: DocumentRef(document_id=doc_id, title="t")},
        )
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
        )
        # The list must round-trip the canonical tuple.
        assert pack.untrusted_content_notice_pinned_phrases == list(
            UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES
        )

    def test_answer_response_carries_pinned_phrases_on_refusal(self) -> None:
        """The refusal paths in answer.py also populate the field."""
        from uuid import uuid4

        from cf_knowledge_kiln.agent.answer import _no_evidence_refusal
        from cf_knowledge_kiln.retrieval.types import (
            AnswerRequest,
            ContextPackResponse,
            TokenBudget,
        )

        pack = ContextPackResponse(
            context_pack_id=uuid4(),
            answerable=False,
            confidence="none",
            summary=None,
            recommended_use=None,
            evidence=[],
            warnings=[],
            conflicts=[],
            related_sources=[],
            token_budget=TokenBudget(requested=3000, used_estimate=0),
            requires_human_review=True,
            review_reasons=["no evidence"],
            untrusted_content_notice=UNTRUSTED_CONTENT_NOTICE,
            untrusted_content_notice_id="kiln.untrusted-content.v1",
            untrusted_content_notice_pinned_phrases=list(UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES),
        )
        req = AnswerRequest(query="q", task="t")
        resp = _no_evidence_refusal(pack, answer_id=uuid4(), request=req)
        assert resp.untrusted_content_notice_pinned_phrases == list(
            UNTRUSTED_CONTENT_NOTICE_PINNED_PHRASES
        )


class TestOpenAPISchemaPinsField:
    """The OpenAPI schema must list the new field on both response
    types. The drift test in test_openapi_drift.py already enforces
    Pydantic↔hand-spec property-name parity; we add an explicit
    name pin here so a future drop is loud."""

    def _yaml(self) -> str:
        return (Path(__file__).resolve().parents[2] / "openapi/openapi.yaml").read_text()

    def test_field_appears_on_context_pack_response(self) -> None:
        text = self._yaml()
        # Find the ContextPackResponse block.
        idx = text.index("ContextPackResponse:")
        block = text[idx : idx + 4000]
        assert "untrusted_content_notice_pinned_phrases" in block

    def test_field_appears_on_answer_response(self) -> None:
        text = self._yaml()
        idx = text.index("AnswerResponse:")
        block = text[idx : idx + 6000]
        assert "untrusted_content_notice_pinned_phrases" in block
