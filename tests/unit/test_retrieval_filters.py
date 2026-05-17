"""Unit tests for retrieval/filters.py (Phase 5 slice 1).

These tests verify the generated SQL fragments compile + carry the
intended predicate shape. They don't hit a database — the integration
tier in Phase 5 slice 2 exercises the full CTE end-to-end.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from cf_knowledge_kiln.db.models import Document, DocumentChunk
from cf_knowledge_kiln.retrieval.filters import build_predicates
from cf_knowledge_kiln.retrieval.types import RetrievalFilters


def _compile(preds: list[object], *, literal: bool = True) -> str:
    """Compile a SELECT with the predicates AND'd into the WHERE clause."""
    stmt = select(DocumentChunk.id).join(Document, DocumentChunk.document_id == Document.id)
    if preds:
        stmt = stmt.where(*preds)  # type: ignore[arg-type]
    kwargs: dict[str, object] = {"literal_binds": True} if literal else {}
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs=kwargs))


class TestBuildPredicates:
    def test_empty_filters_returns_no_predicates(self) -> None:
        assert build_predicates(RetrievalFilters()) == []

    def test_status_in_clause(self) -> None:
        sql = _compile(build_predicates(RetrievalFilters(status=["active", "approved"])))
        assert "documents.status IN" in sql
        assert "'active'" in sql
        assert "'approved'" in sql

    def test_repo_in_clause(self) -> None:
        sql = _compile(build_predicates(RetrievalFilters(repo=["org/handbook"])))
        assert "documents.repo IN" in sql
        assert "'org/handbook'" in sql

    def test_path_prefix_uses_like(self) -> None:
        sql = _compile(build_predicates(RetrievalFilters(path_prefix=["security/"])))
        assert "documents.path LIKE" in sql
        assert "'security/" in sql

    def test_path_prefix_escapes_user_wildcards(self) -> None:
        """A prefix containing % must not become a wildcard."""
        sql = _compile(build_predicates(RetrievalFilters(path_prefix=["a%b"])))
        # SQLAlchemy autoescape renders this with an ESCAPE clause.
        assert "ESCAPE" in sql

    def test_last_reviewed_after_emits_ge_predicate(self) -> None:
        sql = _compile(build_predicates(RetrievalFilters(last_reviewed_after=date(2025, 1, 1))))
        assert "documents.last_reviewed >=" in sql
        assert "'2025-01-01'" in sql

    def test_owner_in_clause(self) -> None:
        sql = _compile(build_predicates(RetrievalFilters(owner=["platform", "security"])))
        assert "documents.owner IN" in sql

    def test_combined_filters_are_anded(self) -> None:
        sql = _compile(
            build_predicates(
                RetrievalFilters(
                    status=["active"],
                    repo=["org/a"],
                    last_reviewed_after=date(2024, 1, 1),
                )
            )
        )
        # All three pieces appear; SQLAlchemy joins them with AND in where().
        assert "documents.status IN" in sql
        assert "documents.repo IN" in sql
        assert "documents.last_reviewed >=" in sql
        assert " AND " in sql

    def test_control_id_predicate_compiles(self) -> None:
        # JSONB literals can't render inline; just verify the predicate
        # builds + touches the metadata column without raising.
        preds = build_predicates(RetrievalFilters(control_id=["AC-2", "AC-3"]))
        assert len(preds) == 1
        sql = _compile(preds, literal=False)
        assert "documents.metadata" in sql

    def test_tags_predicate_uses_jsonb_overlap(self) -> None:
        preds = build_predicates(RetrievalFilters(tags=["runbook"]))
        assert len(preds) == 1
        sql = _compile(preds, literal=False)
        # Postgres "exists any" operator must appear in the rendered SQL.
        assert "?|" in sql
        # Both the chunk-side and doc-side metadata columns participate.
        assert "document_chunks.metadata" in sql
        assert "documents.metadata" in sql
