"""Pydantic model contracts for retrieval/types.py.

Drift between these models and openapi.yaml is caught by the
OpenAPI drift test (test_openapi_drift.py); this file just verifies
the in-process model behavior — accepted/rejected inputs, defaults,
extras-forbidden.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cf_knowledge_kiln.retrieval import Conflict, RetrievalFilters, Warning
from cf_knowledge_kiln.retrieval.types import (
    MAX_FILTER_ITEMS,
    MAX_QUERY_LENGTH,
    ResultCard,
    SearchRequest,
)


class TestRetrievalFilters:
    def test_empty_filters_is_valid(self) -> None:
        filters = RetrievalFilters()
        assert filters.status is None
        assert filters.repo is None
        assert filters.last_reviewed_after is None

    def test_filter_list_rejects_over_limit(self) -> None:
        """A pathological filter list explodes the SQL IN (...) — refuse it."""
        with pytest.raises(ValidationError):
            RetrievalFilters(repo=["r"] * (MAX_FILTER_ITEMS + 1))
        with pytest.raises(ValidationError):
            RetrievalFilters(tags=["t"] * (MAX_FILTER_ITEMS + 1))

    def test_filter_list_accepts_at_limit(self) -> None:
        filters = RetrievalFilters(repo=["r"] * MAX_FILTER_ITEMS)
        assert filters.repo is not None and len(filters.repo) == MAX_FILTER_ITEMS

    def test_status_must_be_in_enum(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalFilters(status=["live"])  # type: ignore[list-item]

    def test_status_accepts_full_enum(self) -> None:
        filters = RetrievalFilters(
            status=["active", "approved", "draft", "deprecated", "archived", "superseded"]
        )
        assert filters.status is not None
        assert len(filters.status) == 6

    def test_last_reviewed_after_parses_iso_date(self) -> None:
        filters = RetrievalFilters(last_reviewed_after=date(2025, 1, 1))
        assert filters.last_reviewed_after == date(2025, 1, 1)

    def test_extras_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalFilters(unknown_field="x")  # type: ignore[call-arg]


class TestSearchRequest:
    def test_minimal_valid(self) -> None:
        req = SearchRequest(query="how do I deploy")
        assert req.query == "how do I deploy"
        assert req.max_results == 10

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="")

    def test_query_rejects_over_max_length(self) -> None:
        """A multi-MB query forces unbounded FTS + embedding compute."""
        with pytest.raises(ValidationError):
            SearchRequest(query="x" * (MAX_QUERY_LENGTH + 1))

    def test_query_accepts_at_max_length(self) -> None:
        req = SearchRequest(query="x" * MAX_QUERY_LENGTH)
        assert len(req.query) == MAX_QUERY_LENGTH

    def test_extras_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="q", surprise="oops")  # type: ignore[call-arg]


class TestWarning:
    def test_required_fields(self) -> None:
        w = Warning(type="stale_source", message="too old")
        assert w.type == "stale_source"
        assert w.source_id is None

    def test_type_must_be_in_enum(self) -> None:
        with pytest.raises(ValidationError):
            Warning(type="not_a_warning_type", message="x")  # type: ignore[arg-type]

    def test_source_id_optional(self) -> None:
        uid = uuid4()
        w = Warning(type="deprecated_source", message="x", source_id=uid)
        assert w.source_id == uid


class TestResultCardStatus:
    """#203 regression: ResultCard.status accepts any string, not just the
    Status Literal. Real corpora ship statuses like 'reference',
    'canonical', 'running' that aren't in the kiln's recommended set;
    rejecting them used to crash the whole /v1/search request with a
    Pydantic ValidationError → HTTP 500."""

    def _card(self, *, status: str) -> ResultCard:
        return ResultCard(
            chunk_id=uuid4(),
            document_id=uuid4(),
            title="t",
            excerpt="e",
            status=status,
            score=0.5,
        )

    def test_accepts_kiln_recommended_status(self) -> None:
        card = self._card(status="active")
        assert card.status == "active"

    @pytest.mark.parametrize(
        "status",
        # Statuses observed in the homelab-iac corpus that USED to 500
        # /v1/search per #203. All must round-trip verbatim now.
        ["reference", "canonical", "running", "ready", "open", "proposal", "implemented"],
    )
    def test_accepts_corpus_native_status(self, status: str) -> None:
        card = self._card(status=status)
        assert card.status == status

    def test_status_is_required(self) -> None:
        with pytest.raises(ValidationError):
            ResultCard(  # type: ignore[call-arg]
                chunk_id=uuid4(),
                document_id=uuid4(),
                title="t",
                excerpt="e",
                score=0.5,
            )


class TestConflict:
    def test_requires_two_or_more_source_ids(self) -> None:
        with pytest.raises(ValidationError):
            Conflict(topic="t", source_ids=[uuid4()])

    def test_accepts_two_or_more(self) -> None:
        a, b = uuid4(), uuid4()
        c = Conflict(topic="Deployment.Web", source_ids=[a, b])
        assert c.topic == "Deployment.Web"
        assert set(c.source_ids) == {a, b}

    def test_description_optional(self) -> None:
        c = Conflict(topic="t", source_ids=[uuid4(), uuid4()])
        assert c.description is None
