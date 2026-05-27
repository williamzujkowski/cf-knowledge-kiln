"""Pins the #340 editorial-copy pass.

The kiln UI voice is documented in docs/copy-voice.md. These tests
guard against regressions of the system-y voice we just removed:
'Error', 'Thanks', 'Success' as bare labels are forbidden in the
templates the audit reviewed.

This is a textual snapshot test — if the visible copy changes,
update the canonical phrase in docs/copy-voice.md and the test
together so the doc + the prose can't drift.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src/cf_knowledge_kiln/api/templates"
_COPY_VOICE = _REPO / "docs/copy-voice.md"


class TestFeedbackAckVoice:
    def test_uses_noted_not_thanks(self) -> None:
        """'Thanks' was the system-y voice the audit flagged.
        'Noted' carries the editorial gloss the rest of the UI uses."""
        text = (_TEMPLATES / "_feedback_ack.html").read_text()
        assert "Noted" in text
        # Regression guard: 'Thanks' is the system-y phrase we removed.
        assert "Thanks" not in text


class TestFeedbackErrorVoice:
    def test_uses_couldnt_record_not_failed(self) -> None:
        text = (_TEMPLATES / "_feedback_error.html").read_text()
        # The smart-quote apostrophe is the canonical form.
        assert "Couldn&rsquo;t record this" in text
        # Regression guard: bare 'Feedback failed' is the system-y
        # phrase we removed.
        assert "Feedback failed" not in text


class TestErrorFragmentVoice:
    def test_label_variable_supported(self) -> None:
        """The template must support a {{ label }} variable so callers
        can name the failure mode without inventing strings inline."""
        text = (_TEMPLATES / "_error.html").read_text()
        assert "{% if label %}" in text or "{%- if label -%}" in text
        # The fallback phrase the audit recommended.
        assert "Couldn&rsquo;t complete this" in text

    def test_label_passed_at_call_sites(self) -> None:
        """The three call sites in api/web.py for _error.html should
        pass an explicit label."""
        web_py = (_REPO / "src/cf_knowledge_kiln/api/web.py").read_text()
        # All three error renders now carry a label kwarg.
        assert '"label": "Too many requests"' in web_py
        assert '"label": "Query too long"' in web_py
        # ASCII apostrophe in the Python source (ruff RUF001 forbids
        # the curly mark in string literals).
        assert "Couldn't reach the engine" in web_py


class TestCopyVoiceDocExists:
    def test_doc_present(self) -> None:
        """The voice doc itself is the spec the tests above reference;
        it must exist so a future contributor can reach it."""
        assert _COPY_VOICE.exists()
        text = _COPY_VOICE.read_text()
        # Sanity: doc covers the canonical phrases above.
        for phrase in (
            "Noted",
            "Couldn't record this",
            "Couldn't reach the engine",
            "Too many requests",
            "Query too long",
        ):
            assert phrase in text, f"copy-voice.md must document the canonical phrase: {phrase!r}"
