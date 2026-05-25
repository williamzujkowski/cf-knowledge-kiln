"""Unit tests for :func:`cf_knowledge_kiln.api.views.split_warnings` (#257).

The split function buckets engine warnings into (query-global, per-document)
so the search-results template can render per-document warnings inline on
the matching result card. Critical for security/compliance decision safety —
a warning floating at the top of the list, disconnected from the card that
triggered it, is worse than no warning for those workflows.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from cf_knowledge_kiln.api.views import (
    humanize_warning,
    split_warnings,
    warning_severity,
)


class _Warning:
    """Minimal stand-in for the engine's Warning Pydantic model."""

    def __init__(self, type: str, message: str = "", source_id: Any = None) -> None:
        self.type = type
        self.message = message
        self.source_id = source_id


class TestHumanizeWarning:
    def test_includes_source_id_when_present(self) -> None:
        sid = uuid.uuid4()
        out = humanize_warning(_Warning("stale_source", "msg", source_id=sid))
        assert out["source_id"] == str(sid)

    def test_source_id_is_none_when_absent(self) -> None:
        out = humanize_warning(_Warning("weak_evidence", "msg"))
        assert out["source_id"] is None

    def test_severity_included(self) -> None:
        out = humanize_warning(_Warning("sensitive_content"))
        assert out["severity"] == "blocking"

    def test_spec_mandated_copy_preserved(self) -> None:
        """Spec voice (user-journeys.md) survives the new fields."""
        out = humanize_warning(_Warning("weak_evidence", "raw"))
        assert "I found related content" in out["message"]
        assert out["prefix"] == "Confidence is low —"


class TestWarningSeverity:
    @pytest.mark.parametrize(
        ("wtype", "expected"),
        [
            ("stale_source", "advisory"),
            ("deprecated_source", "warning"),
            ("query_normalized", "advisory"),
            ("weak_evidence", "warning"),
            ("isolated_match", "warning"),
            ("conflicting_sources", "warning"),
            ("prompt_injection_pattern", "blocking"),
            ("sensitive_content", "blocking"),
        ],
    )
    def test_known_types(self, wtype: str, expected: str) -> None:
        assert warning_severity(wtype) == expected

    def test_unknown_type_defaults_to_advisory(self) -> None:
        """A future warning surfaces quietly until the UI catches up.

        Defaulting to 'blocking' would scream at operators about every
        new warning type before we'd defined the visual treatment — not
        what 'do no harm' looks like for an additive emitter change.
        """
        assert warning_severity("future_warning_xyz") == "advisory"


class TestSplitWarnings:
    def test_warning_with_visible_source_attaches_to_doc(self) -> None:
        doc_id = uuid.uuid4()
        warnings = [
            humanize_warning(_Warning("stale_source", source_id=doc_id)),
        ]
        global_warnings, per_doc = split_warnings(warnings, {str(doc_id)})
        assert global_warnings == []
        assert str(doc_id) in per_doc
        assert per_doc[str(doc_id)][0]["type"] == "stale_source"

    def test_warning_without_source_stays_global(self) -> None:
        warnings = [
            humanize_warning(_Warning("weak_evidence", message="low")),
        ]
        global_warnings, per_doc = split_warnings(warnings, set())
        assert len(global_warnings) == 1
        assert per_doc == {}

    def test_warning_with_source_not_in_visible_results_stays_global(self) -> None:
        """A warning about a chunk that didn't surface in the top-K
        belongs at the top of the page, not lost in the void."""
        invisible_doc = uuid.uuid4()
        warnings = [
            humanize_warning(_Warning("stale_source", source_id=invisible_doc)),
        ]
        global_warnings, per_doc = split_warnings(warnings, {"different-uuid"})
        assert len(global_warnings) == 1
        assert per_doc == {}

    def test_multiple_warnings_per_doc_grouped(self) -> None:
        doc_id = uuid.uuid4()
        warnings = [
            humanize_warning(_Warning("stale_source", source_id=doc_id)),
            humanize_warning(_Warning("deprecated_source", source_id=doc_id)),
        ]
        global_warnings, per_doc = split_warnings(warnings, {str(doc_id)})
        assert global_warnings == []
        assert len(per_doc[str(doc_id)]) == 2

    def test_mixed_split(self) -> None:
        """Realistic shape: some per-doc, some global, single bucket pass."""
        doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
        warnings = [
            humanize_warning(_Warning("weak_evidence")),  # global
            humanize_warning(_Warning("stale_source", source_id=doc_a)),  # to A
            humanize_warning(_Warning("conflicting_sources")),  # global
            humanize_warning(_Warning("sensitive_content", source_id=doc_b)),  # to B
        ]
        global_warnings, per_doc = split_warnings(
            warnings, {str(doc_a), str(doc_b)}
        )
        assert {w["type"] for w in global_warnings} == {
            "weak_evidence",
            "conflicting_sources",
        }
        assert per_doc[str(doc_a)][0]["type"] == "stale_source"
        assert per_doc[str(doc_b)][0]["type"] == "sensitive_content"
