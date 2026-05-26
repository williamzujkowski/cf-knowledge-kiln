"""Source-text tests for the kiln-app.js AT-announcement wiring (#314).

The repo has no JS test runner — convention is source-text grep
assertions on the bundle (same pattern as
:mod:`tests.unit.test_feedback_error_chrome`). These tests pin
that the JS dispatch wires exist and reference the right DOM
selectors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

KILN_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cf_knowledge_kiln"
    / "api"
    / "static"
    / "kiln-app.js"
)


@pytest.fixture(scope="module")
def kiln_app_source() -> str:
    return KILN_APP_JS.read_text(encoding="utf-8")


class TestFeedbackAckAnnouncement:
    def test_dispatcher_matches_data_feedback_ack(
        self, kiln_app_source: str
    ) -> None:
        assert "data-feedback-ack" in kiln_app_source

    def test_dispatcher_announces_with_feedback_recorded_prefix(
        self, kiln_app_source: str
    ) -> None:
        assert "Feedback recorded: " in kiln_app_source

    def test_dispatcher_normalizes_underscores_in_signal(
        self, kiln_app_source: str
    ) -> None:
        """Underscore-to-space normalization for AT readability."""
        assert "/_/g" in kiln_app_source


class TestPreviewAnnouncement:
    def test_dispatcher_reads_preview_title_selector(
        self, kiln_app_source: str
    ) -> None:
        """The preview title source is .preview-title — pin so a
        future template refactor that renames the class breaks
        this test rather than silently breaking AT."""
        assert ".preview-title" in kiln_app_source

    def test_dispatcher_reads_preview_missing_selector(
        self, kiln_app_source: str
    ) -> None:
        assert ".preview-missing" in kiln_app_source

    def test_dispatcher_announces_preview_loaded(
        self, kiln_app_source: str
    ) -> None:
        assert "Preview loaded: " in kiln_app_source

    def test_dispatcher_announces_preview_unavailable(
        self, kiln_app_source: str
    ) -> None:
        assert "Preview unavailable" in kiln_app_source
