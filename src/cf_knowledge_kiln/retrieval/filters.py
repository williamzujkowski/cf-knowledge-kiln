"""Translate :class:`RetrievalFilters` to SQLAlchemy WHERE-clause fragments.

Per ADR-0009, the hybrid retrieval CTE pushes metadata filters into
both arms before the candidate union. This module is the bridge: take
a :class:`RetrievalFilters` value (Pydantic, validated at the API
edge) and return a list of SQL predicates that the query builder
can ``AND`` into the WHERE clause.

The translator does **not** touch the engine itself — it returns
opaque SQLAlchemy predicate expressions. Callers compose them with
their query.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import or_
from sqlalchemy.sql import ColumnElement

from cf_knowledge_kiln.db.models import Document, DocumentChunk
from cf_knowledge_kiln.retrieval.types import RetrievalFilters


def build_predicates(filters: RetrievalFilters) -> list[ColumnElement[bool]]:
    """Return a list of SQL predicates suitable for ``stmt.where(*preds)``.

    Each filter field that is non-empty becomes one predicate; empty
    filters contribute nothing (which is the same as "no constraint").
    Predicates are independent — callers AND them together at the
    query site, and an empty return list is valid (means: filter
    everything in).
    """
    preds: list[ColumnElement[bool]] = []
    _append_in(preds, Document.status, filters.status)
    _append_in(preds, Document.doc_type, filters.doc_type)
    _append_in(preds, Document.repo, filters.repo)
    _append_in(preds, Document.owner, filters.owner)
    _append_in(preds, Document.system, filters.system)
    _append_in(preds, Document.authority, filters.authority)
    _append_in(preds, Document.sensitivity, filters.sensitivity)
    if filters.path_prefix:
        preds.append(_path_prefix_predicate(filters.path_prefix))
    if filters.last_reviewed_after is not None:
        preds.append(Document.last_reviewed >= filters.last_reviewed_after)
    if filters.control_id:
        preds.append(_control_id_predicate(filters.control_id))
    if filters.tags:
        preds.append(_tags_predicate(filters.tags))
    return preds


def _append_in(
    preds: list[ColumnElement[bool]],
    column: Any,
    values: Sequence[str] | None,
) -> None:
    """Append a ``column IN (values...)`` predicate when ``values`` is non-empty."""
    if values:
        preds.append(column.in_(list(values)))


def _path_prefix_predicate(prefixes: Sequence[str]) -> ColumnElement[bool]:
    """Match documents whose path starts with any of the given prefixes.

    Each prefix is matched with ``LIKE 'prefix%'``. SQLAlchemy escapes
    user wildcards so a prefix containing ``%`` or ``_`` is matched
    literally — the caller can't smuggle LIKE-syntax wildcards through
    the API.
    """
    return or_(*[Document.path.startswith(p, autoescape=True) for p in prefixes])


def _control_id_predicate(controls: Sequence[str]) -> ColumnElement[bool]:
    """Match documents whose JSONB metadata['controls'] array overlaps.

    control_id is a domain concept (NIST 800-53 controls, etc.) carried
    in the document's free-form metadata column. We do not have a
    typed column for it; matching against the JSONB array is the
    contract we expose to callers.
    """
    overlap = _jsonb_array_overlap(Document.extra["controls"], controls)
    direct = Document.extra["controls"].astext.in_(list(controls))
    return cast(ColumnElement[bool], direct | overlap)


def _tags_predicate(tags: Sequence[str]) -> ColumnElement[bool]:
    """Match chunks (or documents) whose JSONB metadata['tags'] overlaps."""
    chunk_match = _jsonb_array_overlap(DocumentChunk.extra["tags"], tags)
    doc_match = _jsonb_array_overlap(Document.extra["tags"], tags)
    return cast(ColumnElement[bool], chunk_match | doc_match)


def _jsonb_array_overlap(
    column: Any, values: Sequence[str]
) -> ColumnElement[bool]:
    """``column ?| ARRAY[values]`` — true if any element of values is in the JSONB array.

    Postgres-specific. The ``?|`` operator is the JSONB "exists any"
    operator. We render it via :func:`sqlalchemy.sql.operators.custom_op`
    so SQLAlchemy doesn't try to translate it to standard SQL.
    """
    return cast(ColumnElement[bool], column.op("?|")(list(values)))


__all__ = ["build_predicates"]
