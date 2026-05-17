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

from pydantic import BaseModel, ConfigDict, Field

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
]
"""Warning code enum — matches openapi.yaml Warning.type."""


class RetrievalFilters(BaseModel):
    """Filters narrowed by the caller before scoring.

    Every field is optional + nullable. An empty list and ``None`` both
    mean "no constraint." The :mod:`cf_knowledge_kiln.retrieval.filters`
    translator turns each non-empty value into a SQL predicate.
    """

    model_config = ConfigDict(extra="forbid")

    status: list[Status] | None = None
    doc_type: list[str] | None = None
    repo: list[str] | None = None
    path_prefix: list[str] | None = None
    owner: list[str] | None = None
    system: list[str] | None = None
    authority: list[str] | None = None
    sensitivity: list[str] | None = None
    control_id: list[str] | None = None
    tags: list[str] | None = None
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


__all__ = [
    "Conflict",
    "RetrievalFilters",
    "Status",
    "Warning",
    "WarningType",
]
