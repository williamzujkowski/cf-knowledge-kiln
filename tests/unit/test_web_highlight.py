"""Unit tests for the result-card helpers in api/web.py + api/forms.py (#117, #129)."""

from __future__ import annotations

from cf_knowledge_kiln.api.views import humanize_warning as _humanize_warning
from cf_knowledge_kiln.api.web import _highlight_excerpt


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

    # ─── #291: phrase-level highlighting when terms co-occur ─────────

    def test_full_query_phrase_wraps_in_single_mark(self) -> None:
        """When the excerpt contains the exact ordered query phrase,
        ONE wrapping <mark> covers the whole span — not three separate
        marks for three separate terms (the pre-#291 behavior). Reads
        as 'this is the phrase you searched for', not 'three unrelated
        terms happened to land near each other'."""
        out = _highlight_excerpt(
            "The postgres connection pool is configured here.",
            "postgres connection pool",
        )
        text = str(out)
        # Exact phrase wrapped as a single mark.
        assert "<mark>postgres connection pool</mark>" in text
        # And explicitly NOT split into three sibling marks.
        assert "<mark>postgres</mark> <mark>connection</mark> <mark>pool</mark>" not in text

    def test_phrase_match_tolerates_whitespace_variants(self) -> None:
        """The phrase regex uses \\s+ between terms so multiple spaces,
        tabs, or newlines between the words still match. Without this,
        any text that wrapped the phrase across line breaks would
        silently fall back to per-term marks."""
        out = _highlight_excerpt(
            "The postgres   connection\tpool spans whitespace.",
            "postgres connection pool",
        )
        text = str(out)
        # One mark covering 'postgres   connection\tpool' with the
        # original whitespace preserved between the terms.
        assert "<mark>postgres   connection\tpool</mark>" in text

    def test_partial_subsequence_marks_subsequence_only(self) -> None:
        """If the full phrase isn't present but a 2-of-3 contiguous
        subsequence is (e.g. 'connection pool' appears in order, but
        'postgres' is elsewhere), the subsequence wraps as one mark
        and 'postgres' gets its own per-term mark. The longest match
        wins at each scan position."""
        out = _highlight_excerpt(
            "Postgres tuning is fine; just check the connection pool size.",
            "postgres connection pool",
        )
        text = str(out)
        # 'Postgres' (single term, separately) wrapped.
        assert "<mark>Postgres</mark>" in text
        # 'connection pool' (2-term subsequence) wrapped as ONE mark.
        assert "<mark>connection pool</mark>" in text

    def test_phrase_absent_falls_back_to_per_term(self) -> None:
        """Backward-compat: when no contiguous subsequence of ≥2 terms
        matches, the per-term highlighter (the pre-#291 behavior)
        carries the result. Pins that the phrase pass is additive,
        not a replacement."""
        out = _highlight_excerpt(
            "pool here, connection elsewhere, postgres way over there",
            "postgres connection pool",
        )
        text = str(out)
        assert "<mark>postgres</mark>" in text
        assert "<mark>connection</mark>" in text
        assert "<mark>pool</mark>" in text

    def test_single_term_query_unchanged_behavior(self) -> None:
        """A 1-term query has no phrase semantics — the new pass
        is a no-op. Pins backward compat for the most common
        query shape."""
        out = _highlight_excerpt("the widgets are here", "widgets")
        text = str(out)
        assert text.count("<mark>") == 1
        assert "<mark>widgets</mark>" in text

    def test_phrase_with_regex_metachars_is_escaped(self) -> None:
        """A query phrase containing regex metachars (., +, *, etc.)
        must not slip into the alternation pattern un-escaped — that
        would change the meaning of the regex and could ReDoS. The
        existing per-term path uses re.escape; the phrase path must
        too."""
        # `.` is regex metachar; `+` is regex metachar.
        out = _highlight_excerpt("The file.ext+suffix lives here.", "file.ext+suffix")
        text = str(out)
        # Literal '.' and '+' are matched; nothing weird like the
        # regex `.` matching any char or `+` causing quantifier errors.
        assert "<mark>file.ext+suffix</mark>" in text

    def test_phrase_html_safety_property(self) -> None:
        """End-to-end XSS guard: a query and a text BOTH carrying
        HTML metachars cannot produce a real tag in output. Pins
        the escape-then-inject pattern survives the phrase code
        path too."""
        out = _highlight_excerpt("Search <img src=x> tag results", "<img src=x>")
        text = str(out)
        # No real <img tag.
        assert "<img" not in text
        # Escaped form appears.
        assert "&lt;img" in text

    def test_long_query_skips_phrase_pass_but_still_marks_per_term(self) -> None:
        """Reviewer-flagged defensive cap: a 50-term adversarial query
        would generate ~1,275 subsequence alternatives and blow regex
        compile time. Past the _PHRASE_TERM_CAP (12), the phrase pass
        is skipped — per-term highlighting carries the result so the
        user still gets a useful mark on each term, just no phrase
        wrapping. Pins the safety valve so a future bypass attempt
        doesn't quietly remove the cap."""
        # 20 distinct non-prefix-colliding terms — use shapes like
        # 'alpha', 'bravo' rather than 'term1'/'term19' (which would
        # have per-term match consume prefixes and confuse the
        # assertion).
        terms = [
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
            "foxtrot",
            "golf",
            "hotel",
            "india",
            "juliet",
            "kilo",
            "lima",
            "mike",
            "november",
            "oscar",
            "papa",
            "quebec",
            "romeo",
            "sierra",
            "tango",
        ]
        query = " ".join(terms)
        text = " ".join(terms)
        out = _highlight_excerpt(text, query)
        result = str(out)
        # Every term still gets a per-term mark (per-term path is
        # always taken regardless of the cap).
        assert "<mark>alpha</mark>" in result
        assert "<mark>tango</mark>" in result
        # And NO multi-term phrase-wrapping mark appears (the phrase
        # pass was skipped past the cap). A wrapping mark would look
        # like "<mark>alpha bravo</mark>" — assert it's absent.
        assert "<mark>alpha bravo</mark>" not in result


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


# ─── #118: filter-form helpers ──────────────────────────────────────


class TestSplitCsv:
    def test_splits_on_commas(self) -> None:
        from cf_knowledge_kiln.api.forms import split_csv

        assert split_csv("foo,bar,baz") == ["foo", "bar", "baz"]

    def test_splits_on_whitespace(self) -> None:
        from cf_knowledge_kiln.api.forms import split_csv

        assert split_csv("foo bar baz") == ["foo", "bar", "baz"]

    def test_splits_on_mixed_separators(self) -> None:
        from cf_knowledge_kiln.api.forms import split_csv

        assert split_csv("foo, bar,  baz   qux") == ["foo", "bar", "baz", "qux"]

    def test_empty_returns_empty_list(self) -> None:
        from cf_knowledge_kiln.api.forms import split_csv

        assert split_csv("") == []
        assert split_csv("   ") == []
        assert split_csv(",,,") == []


class TestParseIsoDate:
    def test_valid_iso_date(self) -> None:
        from datetime import date

        from cf_knowledge_kiln.api.forms import parse_iso_date

        assert parse_iso_date("2026-05-17") == date(2026, 5, 17)

    def test_empty_returns_none(self) -> None:
        from cf_knowledge_kiln.api.forms import parse_iso_date

        assert parse_iso_date("") is None

    def test_invalid_returns_none(self) -> None:
        """Malformed input drops silently — form-side validator catches it."""
        from cf_knowledge_kiln.api.forms import parse_iso_date

        assert parse_iso_date("not-a-date") is None
        assert parse_iso_date("2026-13-99") is None
