"""Unit tests for the result-card helpers in api/web.py (#117)."""

from __future__ import annotations

from cf_knowledge_kiln.api.web import _highlight_excerpt, _humanize_warning


class TestHighlightExcerpt:
    def test_no_query_returns_escaped_text(self) -> None:
        out = _highlight_excerpt("plain text", "")
        assert str(out) == "plain text"

    def test_wraps_matching_term(self) -> None:
        out = _highlight_excerpt("hello widgets world", "widgets")
        assert "<mark>widgets</mark>" in str(out)

    def test_case_insensitive_match(self) -> None:
        out = _highlight_excerpt("Hello Widgets World", "widgets")
        assert "<mark>Widgets</mark>" in str(out)

    def test_short_terms_dropped(self) -> None:
        """Single-letter terms are dropped to avoid stopword noise."""
        out = _highlight_excerpt("a stand-alone widgets", "a widgets")
        assert "<mark>a</mark>" not in str(out)
        assert "<mark>widgets</mark>" in str(out)

    def test_two_letter_acronym_highlighted(self) -> None:
        """#125 reviewer: drop threshold to 2 so CF/DB/OS/AI work."""
        out = _highlight_excerpt("the CF foundation runs DB workloads", "CF DB")
        assert "<mark>CF</mark>" in str(out)
        assert "<mark>DB</mark>" in str(out)

    def test_stopwords_dropped(self) -> None:
        """Common 2-letter stopwords don't pollute the highlight."""
        out = _highlight_excerpt("a moment in the on or by routine", "is of to")
        assert "<mark>" not in str(out)

    def test_html_in_text_is_escaped(self) -> None:
        """User-supplied excerpt text must be HTML-escaped."""
        out = _highlight_excerpt("hello <script>alert(1)</script>", "hello")
        text = str(out)
        # The literal <script> never appears as a real tag.
        assert "<script>" not in text
        assert "&lt;script&gt;" in text
        # The term we did highlight is still wrapped.
        assert "<mark>hello</mark>" in text

    def test_html_in_query_term_is_escaped(self) -> None:
        """A query term containing HTML metachars can't smuggle a tag.

        re.escape() protects against regex injection; escape() on the
        text protects against HTML injection. The end-to-end property:
        no raw <img/<script tag appears in output for any input.
        """
        out = _highlight_excerpt("the <img src=x> tag here", "<img")
        text = str(out)
        assert "<img" not in text
        assert "&lt;img" in text

    def test_amp_in_text_does_not_break_highlight(self) -> None:
        """An & in the text becomes &amp; — highlighter still works after it."""
        out = _highlight_excerpt("foo & bar widgets", "widgets")
        text = str(out)
        assert "&amp;" in text
        assert "<mark>widgets</mark>" in text


class TestHumanizeWarning:
    def test_known_type_uses_spec_copy(self) -> None:
        class _W:
            type = "weak_evidence"
            message = "engine raw"

        out = _humanize_warning(_W())
        assert out["type"] == "weak_evidence"
        assert out["prefix"] == "Confidence is low —"
        assert "no clearly authoritative source" in out["message"]

    def test_empty_override_falls_back_to_engine_message(self) -> None:
        """stale_source carries an empty override → engine raw wins."""

        class _W:
            type = "stale_source"
            message = "Document last reviewed 2024-01-15; older than 365 days."

        out = _humanize_warning(_W())
        assert out["prefix"] == "Source is stale —"
        assert "2024-01-15" in out["message"]

    def test_unknown_type_uses_engine_message_with_no_prefix(self) -> None:
        class _W:
            type = "future_warning_type"
            message = "engine said this"

        out = _humanize_warning(_W())
        assert out["prefix"] == ""
        assert out["message"] == "engine said this"

    def test_missing_attrs_returns_empty_fallback(self) -> None:
        out = _humanize_warning(object())
        assert out["type"] == ""
        assert out["message"] == ""
