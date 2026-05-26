"""Discriminated ``Warning`` variants — engine-internal, per-variant fields (#310).

This module ships **Step 1** of the two-step migration from the flat
``Warning`` shape (carried in :mod:`cf_knowledge_kiln.retrieval.types`)
to a discriminated union with per-variant fields. The migration plan
lives in ADR-0011; the issue is #310.

What this PR does:

* Define nine Pydantic variant models — one per ``WarningType`` code,
  each carrying the fields that today's consumers have to substring-
  parse out of the flat ``message`` line.
* Expose a ``WarningVariant`` discriminated-union alias keyed on
  ``type``, so an engine emitter can declare ``-> WarningVariant`` and
  callers can ``isinstance`` switch on the variant.
* Provide :func:`downgrade_to_flat` and :func:`downgrade_to_flat_list`
  so the engine can keep emitting variants internally while the wire
  boundary (FastAPI response models, OpenAPI schema, telemetry JSONB
  columns) stays the legacy flat ``Warning`` shape. Byte-identical
  output is the contract this PR pins.

What this PR does **not** do:

* Re-shape engine emitters (`ranking.stale_warnings`,
  `_engine_helpers.conflict_warnings`, etc.) to return variants.
  That's the engine-refactor follow-up; this PR only ships the type
  surface + adapter.
* Change the wire schema in ``openapi/openapi.yaml``. The flat
  ``Warning`` shape stays canonical until a future ``/v2/`` PR
  ships the discriminated wire schema.
* Widen ingest to populate ``pattern_id`` / ``classifier_label``.
  Both stay ``str | None = None`` here; widening is a follow-up.

Voice: variants subclass nothing from the flat ``Warning`` —
they share its ``type`` and ``message`` fields by convention but
not by inheritance, so the OpenAPI drift test stays focused on the
public flat shape. The downgrade adapter is the only place that
crosses the boundary.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cf_knowledge_kiln.retrieval.types import Warning

# ─── Variant base + helpers ────────────────────────────────────


class _VariantBase(BaseModel):
    """Common configuration for every variant.

    ``extra='forbid'`` mirrors the flat ``Warning`` and catches
    typos in emitter call sites (a forgotten kwarg surfaces at
    construction time, not at downgrade time).
    """

    model_config = ConfigDict(extra="forbid")


# ─── The nine variants ─────────────────────────────────────────


class StaleSourceWarning(_VariantBase):
    """Source was last reviewed before the freshness threshold (#83 family).

    ``stale_after_days`` is the configured threshold the engine
    compared against; ``last_reviewed`` is the recorded date if
    one was available (older sources without a reviewed-date still
    fire, hence the optional field).
    """

    type: Literal["stale_source"]
    message: str
    source_id: UUID
    stale_after_days: int = Field(gt=0)
    last_reviewed: date | None = None


class DeprecatedSourceWarning(_VariantBase):
    """Source carries a non-current status (#73, deprecated docs flag).

    The flat ``Warning`` lost the specific status (deprecated vs
    archived vs superseded); variants surface it as
    ``document_status`` for consumers that want to route
    "superseded" cards to the successor while keeping "archived"
    in a separate history bucket.
    """

    type: Literal["deprecated_source"]
    message: str
    source_id: UUID
    document_status: Literal["deprecated", "archived", "superseded"]


class ConflictingSourcesWarning(_VariantBase):
    """Multiple sources disagree on the topic (#74).

    ``source_ids`` is the full set the engine clustered as
    conflicting (today's flat shape loses everything but the count,
    surfaced only in the prose ``message``). The downgrade adapter
    intentionally projects ``source_id=None`` on the flat shape so
    the wire stays identical to today's emit path.
    """

    type: Literal["conflicting_sources"]
    message: str
    source_ids: list[UUID] = Field(min_length=2)
    topic: str = Field(min_length=1)


class WeakEvidenceWarning(_VariantBase):
    """Best chunk's score is below the configured floor (#108, #161).

    A single variant covers both shapes today's emitters produce:

    * "No chunks at all" → ``top_score=None``
    * "Best chunk below floor" → ``top_score < score_floor``

    Consumers branch on ``top_score is None`` if they need to
    distinguish; one variant keeps the discriminator (and the
    wire enum) at nine entries instead of ten.
    """

    type: Literal["weak_evidence"]
    message: str
    top_score: float | None
    score_floor: float


class IsolatedMatchWarning(_VariantBase):
    """Top hit alone above the field — possible single-chunk artifact (#227).

    Fires when ``gap = top_score - runner_up_score > drop_threshold``.
    The variant carries the offending top-1 chunk's identity
    (``source_id`` + ``chunk_id``) so a UI can surface "this top
    result is unusually isolated" inline on that card; today's flat
    shape only carries the per-warning ``source_id``.
    """

    type: Literal["isolated_match"]
    message: str
    top_score: float
    runner_up_score: float
    gap: float
    drop_threshold: float
    source_id: UUID
    chunk_id: UUID

    @model_validator(mode="after")
    def _gap_must_exceed_threshold(self) -> IsolatedMatchWarning:
        # Invariant of the engine emitter: it only fires this warning
        # when the gap clears the drop_threshold. Pin the invariant
        # at the boundary so a buggy emitter site can't smuggle a
        # zero-gap warning past consumers.
        if self.gap <= self.drop_threshold:
            raise ValueError(
                "isolated_match: gap must exceed drop_threshold "
                f"(gap={self.gap}, drop_threshold={self.drop_threshold})"
            )
        return self


class PromptInjectionPatternWarning(_VariantBase):
    """A chunk matched an injection-pattern fingerprint (#75).

    ``pattern_id`` is the matched rule name when the upstream
    classifier carries it — ingest doesn't yet propagate this end-
    to-end, so the field is nullable for now. Widening ingest to
    populate it is a follow-up.
    """

    type: Literal["prompt_injection_pattern"]
    message: str
    source_id: UUID
    chunk_id: UUID
    pattern_id: str | None = None


class SensitiveContentWarning(_VariantBase):
    """A chunk matched a sensitive-content classifier (#75, sibling rule).

    ``classifier_label`` is the matched category when upstream
    surfaces it (same ingest-side caveat as ``pattern_id``).
    """

    type: Literal["sensitive_content"]
    message: str
    source_id: UUID
    chunk_id: UUID
    classifier_label: str | None = None


class QueryNormalizedWarning(_VariantBase):
    """Engine stripped chatter / pleasantries from the user's query (#252).

    ``removed_phrases`` is the list the normalizer dropped (e.g.
    ["please", "thanks"]). ``original_query`` / ``normalized_query``
    are nullable because the engine helper that constructs this
    warning may not see both strings — emitters that have them
    populate them.
    """

    type: Literal["query_normalized"]
    message: str
    removed_phrases: list[str] = Field(min_length=1)
    original_query: str | None = None
    normalized_query: str | None = None


class AnswerTruncatedWarning(_VariantBase):
    """The generator hit ``max_answer_tokens`` mid-stream (#310 NEW).

    Replaces the prior misuse of ``weak_evidence`` on the
    ``/v1/answer`` truncation path. ``finish_reason`` is pinned to
    the only value that triggers this warning (generators can
    finish for many reasons; only "length" — token-budget
    exhausted — counts as truncation).
    """

    type: Literal["answer_truncated"]
    message: str
    finish_reason: Literal["length"]
    max_answer_tokens: int = Field(ge=1)


# ─── Discriminated-union alias ────────────────────────────────


WarningVariant = Annotated[
    StaleSourceWarning
    | DeprecatedSourceWarning
    | ConflictingSourcesWarning
    | WeakEvidenceWarning
    | IsolatedMatchWarning
    | PromptInjectionPatternWarning
    | SensitiveContentWarning
    | QueryNormalizedWarning
    | AnswerTruncatedWarning,
    Field(discriminator="type"),
]
"""Discriminated-union alias for engine-internal warning lists.

Pydantic uses ``type`` to pick the concrete variant on
``model_validate``. Engine emitters declare their return type as
``WarningVariant`` (or ``list[WarningVariant]``) so consumers can
``isinstance``-switch on the per-variant fields.
"""


# ─── Downgrade adapter to the flat wire shape ─────────────────


def downgrade_to_flat(variant: WarningVariant) -> Warning:
    """Project a variant onto the flat ``Warning`` shape.

    Drops every per-variant field; preserves ``type``, ``message``,
    and (where the variant carries it) ``source_id``. The flat
    shape is the wire contract for ``/v1/*`` until the ``/v2/``
    PR ships the discriminated schema.

    ``ConflictingSourcesWarning`` carries ``source_ids`` not
    ``source_id`` — the downgrade emits ``source_id=None`` so the
    wire matches today's behavior (conflict warnings have never
    carried a flat ``source_id``; per-document conflict attachment
    is a ``/v2/`` decision).
    """
    return Warning(
        type=variant.type,
        message=variant.message,
        source_id=getattr(variant, "source_id", None),
    )


def downgrade_to_flat_list(variants: list[WarningVariant]) -> list[Warning]:
    """Batch :func:`downgrade_to_flat` over a list.

    Stable ordering: the i-th flat output corresponds to the i-th
    variant input. Empty list in → empty list out.
    """
    return [downgrade_to_flat(v) for v in variants]


__all__ = [
    "AnswerTruncatedWarning",
    "ConflictingSourcesWarning",
    "DeprecatedSourceWarning",
    "IsolatedMatchWarning",
    "PromptInjectionPatternWarning",
    "QueryNormalizedWarning",
    "SensitiveContentWarning",
    "StaleSourceWarning",
    "WarningVariant",
    "WeakEvidenceWarning",
    "downgrade_to_flat",
    "downgrade_to_flat_list",
]
