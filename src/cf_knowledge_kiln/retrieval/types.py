"""Public Pydantic types for retrieval (Phase 5).

These shapes match the hand-authored ``openapi/openapi.yaml`` 1:1 —
the drift test in ``tests/unit/test_openapi_drift.py`` enforces the
match. Keeping the models here (not in the API layer) means the
retrieval engine and the API share one source of truth instead of
diverging dataclasses.

Per ADR-0003: the OpenAPI contract is the canonical interface, the
Pydantic models are the runtime form, and these must agree.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import AnyUrl, BaseModel, ConfigDict, Field

Status = Literal[
    "active",
    "approved",
    "draft",
    "deprecated",
    "archived",
    "superseded",
]
"""Document status enum — matches openapi.yaml schemas (Document, ResultCard)."""

WarningType = Literal[
    "stale_source",
    "deprecated_source",
    "conflicting_sources",
    "weak_evidence",
    "prompt_injection_pattern",
    "sensitive_content",
    "query_normalized",
]
"""Warning code enum — matches openapi.yaml Warning.type."""

Confidence = Literal["high", "medium", "low", "none"]
"""ContextPackResponse confidence enum."""

Relationship = Literal[
    "supersedes",
    "superseded_by",
    "related_standard",
    "also_relevant",
]
"""RelatedSource relationship kind — matches openapi.yaml RelatedSource."""


# ─── Request-input bounds ────────────────────────────────────────────
#
# Upper bounds on caller-supplied request fields. Without these a single
# request can force unbounded work: a multi-MB query string drives
# unbounded FTS tokenization + embedding compute, and a pathological
# filter list explodes the SQL ``IN (...)`` clause. The per-request rate
# limiter does not help — one request is enough. Values are generous for
# every legitimate caller; embedding models truncate well before
# MAX_QUERY_LENGTH. Mirrored as ``maxLength`` / ``maxItems`` in
# openapi/openapi.yaml.

MAX_QUERY_LENGTH = 4096
"""Max characters in a free-text query (``/v1/search`` + context-pack)."""

MAX_TASK_LENGTH = 2048
"""Max characters in a context-pack ``task`` description."""

MAX_FILTER_ITEMS = 100
"""Max item count for any :class:`RetrievalFilters` list field."""


class RetrievalFilters(BaseModel):
    """Filters narrowed by the caller before scoring.

    Every field is optional + nullable. An empty list and ``None`` both
    mean "no constraint." The :mod:`cf_knowledge_kiln.retrieval.filters`
    translator turns each non-empty value into a SQL predicate.
    """

    model_config = ConfigDict(extra="forbid")

    # Every list is capped at MAX_FILTER_ITEMS so a pathological payload
    # can't explode the SQL ``IN (...)`` clause built in retrieval.filters.
    status: list[Status] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    doc_type: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    repo: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    path_prefix: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    owner: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    system: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    authority: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    sensitivity: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    control_id: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    tags: list[str] | None = Field(default=None, max_length=MAX_FILTER_ITEMS)
    last_reviewed_after: date | None = None


class Warning(BaseModel):
    """A retrieval-side advisory attached to a response or chunk.

    See :data:`WarningType` for the closed set of codes. ``source_id``
    is the document_id the warning is about (when applicable).
    """

    model_config = ConfigDict(extra="forbid")

    type: WarningType
    message: str
    source_id: UUID | None = None


class SearchRequest(BaseModel):
    """POST /v1/search request body."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    filters: RetrievalFilters | None = None
    max_results: int = Field(default=10, ge=1, le=50)


class ResultCard(BaseModel):
    """Human-search result card returned by /v1/search."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    document_id: UUID
    title: str
    excerpt: str
    heading_path: list[str] | None = None
    repo: str | None = None
    path: str | None = None
    source_url: AnyUrl | None = None
    commit_sha: str | None = None
    # #203: ``str`` not :data:`Status`. The kiln recommends the
    # :data:`Status` vocabulary, but real corpora ship with status
    # values outside it (e.g. ``"reference"``, ``"canonical"``,
    # ``"running"``). Pinning the response field to a Literal made
    # any chunk with such a status crash the whole /v1/search request
    # with a Pydantic ValidationError → 500. The agent-side
    # :class:`EvidenceChunk` already uses ``str`` for the same reason.
    status: str
    owner: str | None = None
    last_reviewed: date | None = None
    score: float = Field(ge=0)
    warnings: list[Warning] | None = None


class SearchResponse(BaseModel):
    """POST /v1/search response body."""

    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[ResultCard]
    warnings: list[Warning] | None = None


class Conflict(BaseModel):
    """≥2 active sources that touch the same heading_path.

    Phase 5 detection is syntactic (same heading_path, different
    documents). Semantic conflict — "doc A says X, doc B says not-X"
    — is out of scope for Phase 5 (it needs an LLM mediator).
    """

    model_config = ConfigDict(extra="forbid")

    topic: str
    source_ids: list[UUID] = Field(min_length=2)
    description: str | None = None


# ─── Agent context-pack shapes (slice 3) ─────────────────────────────


class ContextPackRequest(BaseModel):
    """Agent-side request shape for POST /v1/agent/context-pack.

    Defaults match openapi.yaml. ``include_*`` flags let the agent
    trim parts of the response it doesn't want (e.g., an agent that
    only consumes evidence text can pass ``include_related_sources=False``).
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(max_length=MAX_TASK_LENGTH)
    query: str = Field(max_length=MAX_QUERY_LENGTH)
    filters: RetrievalFilters | None = None
    max_chunks: int = Field(default=8, ge=1, le=50)
    max_tokens: int = Field(default=3000, ge=100, le=32_000)
    include_summary: bool = True
    include_conflicts: bool = True
    include_related_sources: bool = True
    require_citations: bool = True


class EvidenceChunk(BaseModel):
    """One evidence chunk in an agent context pack.

    ``text`` is the chunk content — agents MUST treat it as source
    evidence, never as instructions (see
    :attr:`ContextPackResponse.untrusted_content_notice`).
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    document_id: UUID
    title: str
    repo: str | None = None
    path: str | None = None
    heading_path: list[str] | None = None
    source_url: AnyUrl | None = None
    commit_sha: str | None = None
    status: str
    authority: str | None = None
    owner: str | None = None
    last_reviewed: date | None = None
    score: float
    text: str


class RelatedSource(BaseModel):
    """A neighbor source surfaced alongside evidence chunks."""

    model_config = ConfigDict(extra="forbid")

    title: str
    document_id: UUID
    relationship: Relationship


class TokenBudget(BaseModel):
    """How the agent's token budget was spent."""

    model_config = ConfigDict(extra="forbid")

    requested: int
    used_estimate: int


class ContextPackResponse(BaseModel):
    """Bounded, cited context pack returned to an agent caller.

    ``requires_human_review`` is the canonical decision from
    :func:`cf_knowledge_kiln.retrieval.ranking.requires_human_review`.
    """

    model_config = ConfigDict(extra="forbid")

    context_pack_id: UUID
    answerable: bool
    confidence: Confidence | None = None
    summary: str | None = None
    recommended_use: str | None = None
    # evidence + warnings are required by openapi.yaml even when empty.
    evidence: list[EvidenceChunk]
    warnings: list[Warning]
    conflicts: list[Conflict] = Field(default_factory=list)
    related_sources: list[RelatedSource] = Field(default_factory=list)
    token_budget: TokenBudget
    requires_human_review: bool
    review_reasons: list[str] = Field(default_factory=list)
    # Required, not optional (#188): AGENTS.md makes the untrusted-content
    # preamble a guarantee for every agent response. A non-optional field
    # means it cannot be omitted at construction or stripped by
    # response_model_exclude_none.
    untrusted_content_notice: str


# ─── /v1/answer shapes (#192 Phase B+C) ────────────────────────────────


class AnswerRequest(BaseModel):
    """POST /v1/answer request body.

    Same retrieval substrate as :class:`ContextPackRequest`, plus
    ``max_answer_tokens`` for the generator. ``task`` is optional —
    the synthesis prompt defaults to a generic "answer the question
    from the cited evidence" when none is supplied.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    task: str | None = Field(default=None, max_length=MAX_TASK_LENGTH)
    filters: RetrievalFilters | None = None
    max_chunks: int = Field(default=8, ge=1, le=20)
    max_answer_tokens: int = Field(default=1024, ge=64, le=4096)


class AnswerTokenBudget(BaseModel):
    """Token accounting for /v1/answer.

    Honest counts: when the generator returns a ``usage`` block, the
    values are exact; otherwise they're ``None`` and callers should
    treat the request as "couldn't measure" rather than "zero used."
    """

    model_config = ConfigDict(extra="forbid")

    requested_max_answer_tokens: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None


class AnswerResponse(BaseModel):
    """LLM-synthesized cited answer returned by /v1/answer.

    ``answer`` is ``None`` on the refusal path (no evidence,
    upstream ``requires_human_review``, or generator content-filter);
    ``refusal_reason`` is populated in that case. ``answerable``
    mirrors the upstream context-pack semantic so an agent can
    distinguish "we tried and synthesized" from "we refused upstream
    or downstream."

    The same evidence, warnings, conflicts, and untrusted-content
    notice flow through from the underlying ContextPackResponse — a
    /v1/answer caller gets the synthesized answer AND the raw
    evidence + warnings, so it can verify the synthesis against
    sources if desired.
    """

    model_config = ConfigDict(extra="forbid")

    answer_id: UUID
    answer: str | None
    answerable: bool
    confidence: Confidence | None = None
    refusal_reason: str | None = None
    # Same shapes as ContextPackResponse — agents that already consume
    # /v1/agent/context-pack can re-use the same parsing code.
    evidence: list[EvidenceChunk]
    warnings: list[Warning]
    conflicts: list[Conflict] = Field(default_factory=list)
    token_budget: AnswerTokenBudget
    requires_human_review: bool
    review_reasons: list[str] = Field(default_factory=list)
    # Generator-side metadata so an audit row can attribute the answer
    # to the model that produced it. ``None`` on refusals that never
    # reached the generator.
    generator_provider: str | None = None
    generator_model: str | None = None
    # Same required preamble as ContextPackResponse (#188).
    untrusted_content_notice: str


__all__ = [
    "MAX_FILTER_ITEMS",
    "MAX_QUERY_LENGTH",
    "MAX_TASK_LENGTH",
    "AnswerRequest",
    "AnswerResponse",
    "AnswerTokenBudget",
    "Confidence",
    "Conflict",
    "ContextPackRequest",
    "ContextPackResponse",
    "EvidenceChunk",
    "RelatedSource",
    "Relationship",
    "ResultCard",
    "RetrievalFilters",
    "SearchRequest",
    "SearchResponse",
    "Status",
    "TokenBudget",
    "Warning",
    "WarningType",
]
