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


class TestRetrievalFilters:
    def test_empty_filters_is_valid(self) -> None:
        filters = RetrievalFilters()
        assert filters.status is None
        assert filters.repo is None
        assert filters.last_reviewed_after is None

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
