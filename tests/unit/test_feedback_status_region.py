"""Pins the #321 fix: feedback ACK has its own live region.

Two independent polite live regions (#search-status for retrieval /
preview / search-count, #feedback-status for feedback ACK) so a
feedback vote landing within the same event-tick as a preview-load
can't overwrite the preceding announcement before AT reads it.

These tests assert two contracts:

* The template renders BOTH live regions, each with the right ARIA
  attributes (role=status, aria-live=polite, aria-atomic=true).
* The JS feedback path targets #feedback-status (or falls back to
  #search-status when the dedicated region is absent — so the JS
  works against an unmigrated template).
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates"
_KILN_APP_JS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln-app.js"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


def _render(env: jinja2.Environment) -> str:
    return env.get_template("search.html").render(
        request=None,
        query=None,
        initial_results=None,
        filters={},
        rail_active_count=0,
    )


class TestTemplateRendersTwoLiveRegions:
    def test_search_status_region_present(self, env: jinja2.Environment) -> None:
        body = _render(env)
        assert 'id="search-status"' in body

    def test_feedback_status_region_present(self, env: jinja2.Environment) -> None:
        body = _render(env)
        assert 'id="feedback-status"' in body

    def test_both_regions_carry_role_status(self, env: jinja2.Environment) -> None:
        body = _render(env)
        # Locate each region's opening tag and check role=status sits
        # inside it (not somewhere else on the page).
        for region_id in ("search-status", "feedback-status"):
            idx = body.index(f'id="{region_id}"')
            open_lt = body.rfind("<", 0, idx)
            close_gt = body.find(">", idx)
            tag = body[open_lt : close_gt + 1]
            assert 'role="status"' in tag, f"{region_id} missing role=status"
            assert 'aria-live="polite"' in tag, f"{region_id} missing aria-live=polite"
            assert 'aria-atomic="true"' in tag, f"{region_id} missing aria-atomic=true"


class TestJsFeedbackPathTargetsDedicatedRegion:
    """The JS routes feedback ACK to #feedback-status. Source-grep
    is fragile (per #316 review note about JS-grep tests) but the
    alternative — a JSDOM harness — is heavy for this single fact.
    Mitigation: pin the EXACT identifier strings the JS looks up,
    so a refactor that drops the dedicated region is caught.
    """

    def test_feedback_helper_function_defined(self) -> None:
        source = _KILN_APP_JS.read_text()
        # The dedicated feedback-status helper must exist by name.
        assert "_setFeedbackStatus" in source, (
            "kiln-app.js must define _setFeedbackStatus() so feedback "
            "ACK can target a region independent of #search-status."
        )

    def test_feedback_helper_targets_feedback_status_first(self) -> None:
        """Helper reads #feedback-status first, falls back to
        #search-status when the dedicated region is absent."""
        source = _KILN_APP_JS.read_text()
        # Within the helper body, ensure both ids appear and
        # feedback-status appears before search-status (the fallback).
        # We grep textually because the file has multiple references
        # to #search-status; the helper is a small block we can pin.
        helper_start = source.index("_setFeedbackStatus")
        helper_window = source[helper_start : helper_start + 400]
        assert "feedback-status" in helper_window
        # The fallback string must also appear within the helper body
        # so the helper degrades gracefully against an unmigrated
        # template; ordering: feedback-status first.
        feedback_pos = helper_window.index("feedback-status")
        search_pos = helper_window.index("search-status")
        assert feedback_pos < search_pos, (
            "feedback-status MUST come before search-status in the helper "
            "(dedicated region tried first, fallback second)."
        )

    def test_feedback_ack_path_uses_feedback_helper_not_status_helper(self) -> None:
        """The data-feedback-ack handler block must call
        _setFeedbackStatus (not _setStatus). A refactor that
        accidentally reuses _setStatus would re-create the #321
        collision."""
        source = _KILN_APP_JS.read_text()
        # Locate the data-feedback-ack handler and confirm the
        # _setFeedbackStatus call is inside its block. The block is
        # short — bound by the next blank line + close-brace.
        idx = source.index('matches?.("[data-feedback-ack]")')
        # Look ahead ~300 chars — covers the if-body.
        block = source[idx : idx + 400]
        assert "_setFeedbackStatus" in block, (
            "data-feedback-ack handler must call _setFeedbackStatus "
            "(not _setStatus) so the announcement targets the dedicated region."
        )


class TestFeedbackStatusDistinctFromSearchStatus:
    """Belt-and-braces: ensure the two regions are not the same
    element (would happen if someone accidentally reused the id)."""

    def test_two_distinct_ids(self, env: jinja2.Environment) -> None:
        body = _render(env)
        # Each id must occur exactly once in the rendered page —
        # duplicate ids are invalid HTML and would re-create the
        # collision with worse semantics.
        assert body.count('id="search-status"') == 1
        assert body.count('id="feedback-status"') == 1
