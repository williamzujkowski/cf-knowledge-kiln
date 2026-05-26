"""Discriminated Warning union — per-variant Pydantic models + downgrade adapter (#310).

These pin the engine-side warning shapes that the wire-side flat
``Warning`` is downgraded from. The flat shape on the wire stays
unchanged in this PR (Step 1 of the migration plan in ADR-0011);
the variants are introduced internally so a future ``/v2/`` PR can
ship the discriminated schema without re-shaping engine code.

Each variant exposes:

* ``type`` — Literal[<variant_code>], serves as the discriminator
* ``message`` — human-readable line (the original flat-shape carrier)
* per-variant fields — the new typed data that today's consumers
  substring-parse out of ``message``

The adapter :func:`downgrade_to_flat` projects any variant onto the
flat ``Warning`` shape (drops per-variant fields, keeps
``type``/``message``/``source_id``) so the wire boundary is
byte-identical to today.
"""

from __future__ import annotations

from datetime import date
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cf_knowledge_kiln.retrieval.types import Warning, WarningType
from cf_knowledge_kiln.retrieval.warning_variants import (
    AnswerTruncatedWarning,
    ConflictingSourcesWarning,
    DeprecatedSourceWarning,
    IsolatedMatchWarning,
    PromptInjectionPatternWarning,
    QueryNormalizedWarning,
    SensitiveContentWarning,
    StaleSourceWarning,
    WarningVariant,
    WeakEvidenceWarning,
    downgrade_to_flat,
    downgrade_to_flat_list,
)

# ─── Per-variant required + optional field pins ────────────────


class TestStaleSourceWarning:
    def test_required_fields(self) -> None:
        src = uuid4()
        w = StaleSourceWarning(
            type="stale_source",
            message="Source last reviewed 2024-01-15",
            source_id=src,
            stale_after_days=180,
        )
        assert w.type == "stale_source"
        assert w.source_id == src
        assert w.stale_after_days == 180
        assert w.last_reviewed is None  # optional

    def test_last_reviewed_optional(self) -> None:
        w = StaleSourceWarning(
            type="stale_source",
            message="m",
            source_id=uuid4(),
            stale_after_days=180,
            last_reviewed=date(2024, 1, 15),
        )
        assert w.last_reviewed == date(2024, 1, 15)

    def test_missing_source_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaleSourceWarning(  # type: ignore[call-arg]
                type="stale_source", message="m", stale_after_days=180
            )

    def test_stale_after_days_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            StaleSourceWarning(
                type="stale_source",
                message="m",
                source_id=uuid4(),
                stale_after_days=0,
            )


class TestDeprecatedSourceWarning:
    def test_required_fields(self) -> None:
        src = uuid4()
        w = DeprecatedSourceWarning(
            type="deprecated_source",
            message="Document is deprecated",
            source_id=src,
            document_status="deprecated",
        )
        assert w.document_status == "deprecated"

    def test_document_status_accepts_three_values(self) -> None:
        for status in ("deprecated", "archived", "superseded"):
            w = DeprecatedSourceWarning(
                type="deprecated_source",
                message="m",
                source_id=uuid4(),
                document_status=status,  # type: ignore[arg-type]
            )
            assert w.document_status == status

    def test_active_status_rejected(self) -> None:
        # The variant only covers the three non-current statuses.
        with pytest.raises(ValidationError):
            DeprecatedSourceWarning(
                type="deprecated_source",
                message="m",
                source_id=uuid4(),
                document_status="active",  # type: ignore[arg-type]
            )


class TestConflictingSourcesWarning:
    def test_required_fields(self) -> None:
        ids = [uuid4(), uuid4()]
        w = ConflictingSourcesWarning(
            type="conflicting_sources",
            message="Sources disagree",
            source_ids=ids,
            topic="connection pooling",
        )
        assert w.source_ids == ids
        assert w.topic == "connection pooling"

    def test_at_least_two_source_ids_required(self) -> None:
        # A conflict needs at least two sources by definition.
        with pytest.raises(ValidationError):
            ConflictingSourcesWarning(
                type="conflicting_sources",
                message="m",
                source_ids=[uuid4()],
                topic="x",
            )

    def test_topic_required(self) -> None:
        with pytest.raises(ValidationError):
            ConflictingSourcesWarning(  # type: ignore[call-arg]
                type="conflicting_sources",
                message="m",
                source_ids=[uuid4(), uuid4()],
            )


class TestWeakEvidenceWarning:
    def test_top_score_can_be_none(self) -> None:
        # "No chunks at all" path — top_score is None.
        w = WeakEvidenceWarning(
            type="weak_evidence",
            message="No evidence above the configured floor.",
            top_score=None,
            score_floor=0.46,
        )
        assert w.top_score is None
        assert w.score_floor == 0.46

    def test_top_score_below_floor(self) -> None:
        w = WeakEvidenceWarning(
            type="weak_evidence",
            message="Best chunk too weak",
            top_score=0.31,
            score_floor=0.46,
        )
        assert w.top_score == 0.31


class TestIsolatedMatchWarning:
    def test_required_fields(self) -> None:
        src, chunk = uuid4(), uuid4()
        w = IsolatedMatchWarning(
            type="isolated_match",
            message="Top hit is alone above the field",
            top_score=0.9,
            runner_up_score=0.4,
            gap=0.5,
            drop_threshold=0.4,
            source_id=src,
            chunk_id=chunk,
        )
        assert w.source_id == src
        assert w.chunk_id == chunk

    def test_gap_must_exceed_drop_threshold(self) -> None:
        # The warning's reason-to-fire is gap > drop_threshold.
        # If the input violates that, the variant must refuse — it's
        # an invariant of the engine emitter, not of the consumer.
        with pytest.raises(ValidationError):
            IsolatedMatchWarning(
                type="isolated_match",
                message="m",
                top_score=0.5,
                runner_up_score=0.45,
                gap=0.05,
                drop_threshold=0.4,
                source_id=uuid4(),
                chunk_id=uuid4(),
            )


class TestPromptInjectionPatternWarning:
    def test_required_fields(self) -> None:
        src, chunk = uuid4(), uuid4()
        w = PromptInjectionPatternWarning(
            type="prompt_injection_pattern",
            message="Caution — pattern matched",
            source_id=src,
            chunk_id=chunk,
        )
        assert w.source_id == src
        assert w.chunk_id == chunk
        assert w.pattern_id is None  # optional, ingest follow-up

    def test_pattern_id_optional(self) -> None:
        w = PromptInjectionPatternWarning(
            type="prompt_injection_pattern",
            message="m",
            source_id=uuid4(),
            chunk_id=uuid4(),
            pattern_id="ignore-previous-instructions",
        )
        assert w.pattern_id == "ignore-previous-instructions"


class TestSensitiveContentWarning:
    def test_required_fields(self) -> None:
        src, chunk = uuid4(), uuid4()
        w = SensitiveContentWarning(
            type="sensitive_content",
            message="Sensitive content",
            source_id=src,
            chunk_id=chunk,
        )
        assert w.source_id == src
        assert w.chunk_id == chunk
        assert w.classifier_label is None  # optional, ingest follow-up


class TestQueryNormalizedWarning:
    def test_required_fields(self) -> None:
        w = QueryNormalizedWarning(
            type="query_normalized",
            message="Query was normalized",
            removed_phrases=["please", "thanks"],
        )
        assert w.removed_phrases == ["please", "thanks"]
        assert w.original_query is None
        assert w.normalized_query is None

    def test_optional_query_strings(self) -> None:
        w = QueryNormalizedWarning(
            type="query_normalized",
            message="m",
            removed_phrases=["pls"],
            original_query="pls how to deploy",
            normalized_query="how to deploy",
        )
        assert w.original_query == "pls how to deploy"
        assert w.normalized_query == "how to deploy"

    def test_removed_phrases_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            QueryNormalizedWarning(
                type="query_normalized",
                message="m",
                removed_phrases=[],
            )


class TestAnswerTruncatedWarning:
    def test_required_fields(self) -> None:
        w = AnswerTruncatedWarning(
            type="answer_truncated",
            message="Answer was truncated at the configured limit.",
            finish_reason="length",
            max_answer_tokens=512,
        )
        assert w.finish_reason == "length"
        assert w.max_answer_tokens == 512

    def test_max_answer_tokens_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            AnswerTruncatedWarning(
                type="answer_truncated",
                message="m",
                finish_reason="length",
                max_answer_tokens=0,
            )

    def test_finish_reason_pinned_to_length(self) -> None:
        # The variant fires ONLY when the generator stops because the
        # token budget ran out. Any other reason isn't a truncation.
        with pytest.raises(ValidationError):
            AnswerTruncatedWarning(
                type="answer_truncated",
                message="m",
                finish_reason="stop",  # type: ignore[arg-type]
                max_answer_tokens=512,
            )


# ─── Discriminated union semantics ─────────────────────────────


class TestWarningTypeLiteralIncludesAllVariants:
    """The shared ``WarningType`` Literal must list every variant code
    exactly once. If a new variant lands without widening the Literal,
    OpenAPI drift + Pydantic validation will silently disagree.
    """

    def test_literal_covers_nine_codes(self) -> None:
        codes = set(get_args(WarningType))
        assert codes == {
            "stale_source",
            "deprecated_source",
            "conflicting_sources",
            "weak_evidence",
            "isolated_match",
            "prompt_injection_pattern",
            "sensitive_content",
            "query_normalized",
            "answer_truncated",
        }


# ─── Downgrade adapter ─────────────────────────────────────────


def _make_variant_examples() -> list[tuple[WarningVariant, dict[str, object]]]:
    """Build one of every variant + the flat dict the wire MUST see.

    Returns (variant, expected_flat_dict) pairs so the same fixtures
    drive every downgrade-shape test.
    """
    src1, src2 = uuid4(), uuid4()
    chunk = uuid4()
    return [
        (
            StaleSourceWarning(
                type="stale_source",
                message="s",
                source_id=src1,
                stale_after_days=180,
            ),
            {"type": "stale_source", "message": "s", "source_id": src1},
        ),
        (
            DeprecatedSourceWarning(
                type="deprecated_source",
                message="d",
                source_id=src1,
                document_status="deprecated",
            ),
            {"type": "deprecated_source", "message": "d", "source_id": src1},
        ),
        (
            ConflictingSourcesWarning(
                type="conflicting_sources",
                message="c",
                source_ids=[src1, src2],
                topic="x",
            ),
            # NOTE: source_id is None on the flat shape; today's wire
            # contract emits no source_id for conflict warnings, so
            # the downgrade preserves that exactly.
            {"type": "conflicting_sources", "message": "c", "source_id": None},
        ),
        (
            WeakEvidenceWarning(
                type="weak_evidence",
                message="w",
                top_score=None,
                score_floor=0.46,
            ),
            {"type": "weak_evidence", "message": "w", "source_id": None},
        ),
        (
            IsolatedMatchWarning(
                type="isolated_match",
                message="i",
                top_score=0.9,
                runner_up_score=0.4,
                gap=0.5,
                drop_threshold=0.4,
                source_id=src1,
                chunk_id=chunk,
            ),
            {"type": "isolated_match", "message": "i", "source_id": src1},
        ),
        (
            PromptInjectionPatternWarning(
                type="prompt_injection_pattern",
                message="p",
                source_id=src1,
                chunk_id=chunk,
            ),
            {"type": "prompt_injection_pattern", "message": "p", "source_id": src1},
        ),
        (
            SensitiveContentWarning(
                type="sensitive_content",
                message="se",
                source_id=src1,
                chunk_id=chunk,
            ),
            {"type": "sensitive_content", "message": "se", "source_id": src1},
        ),
        (
            QueryNormalizedWarning(
                type="query_normalized",
                message="q",
                removed_phrases=["please"],
            ),
            {"type": "query_normalized", "message": "q", "source_id": None},
        ),
        (
            AnswerTruncatedWarning(
                type="answer_truncated",
                message="a",
                finish_reason="length",
                max_answer_tokens=512,
            ),
            {"type": "answer_truncated", "message": "a", "source_id": None},
        ),
    ]


class TestDowngradeToFlat:
    """The adapter projects variants onto the flat shape. Per-variant
    fields are DROPPED — they live on the engine side until the /v2/
    schema ships them on the wire."""

    @pytest.mark.parametrize("variant, expected", _make_variant_examples())
    def test_downgrade_preserves_type_message_source_id(
        self, variant: WarningVariant, expected: dict[str, object]
    ) -> None:
        flat = downgrade_to_flat(variant)
        assert isinstance(flat, Warning)
        assert flat.type == expected["type"]
        assert flat.message == expected["message"]
        assert flat.source_id == expected["source_id"]

    @pytest.mark.parametrize("variant, _expected", _make_variant_examples())
    def test_downgrade_drops_per_variant_fields(
        self, variant: WarningVariant, _expected: dict[str, object]
    ) -> None:
        # The flat shape has exactly 3 fields. Anything else on the
        # variant (stale_after_days, top_score, removed_phrases, …)
        # must not survive the downgrade.
        flat = downgrade_to_flat(variant)
        assert set(flat.model_dump(exclude_none=False).keys()) == {
            "type",
            "message",
            "source_id",
        }

    def test_downgrade_list_returns_flat_list(self) -> None:
        variants = [v for v, _ in _make_variant_examples()]
        flats = downgrade_to_flat_list(variants)
        assert len(flats) == len(variants)
        assert all(isinstance(f, Warning) for f in flats)

    def test_downgrade_empty_list(self) -> None:
        assert downgrade_to_flat_list([]) == []
