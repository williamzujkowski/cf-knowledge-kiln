"""Unit tests for the shared header sanitizer (#309).

Both ``X-Request-ID`` (PR #265) and ``Idempotency-Key`` (PR #309)
parse opaque inbound strings through the same sanitizer. Sharing
one helper means the two headers can't drift on what "valid"
means; this test pins the contract.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.api._header_sanitize import sanitize_opaque_header


class TestSanitizeOpaqueHeader:
    """Pin the trim → reject-empty → truncate → scrub contract."""

    def test_none_returns_none(self) -> None:
        assert sanitize_opaque_header(None) is None

    def test_empty_returns_none(self) -> None:
        assert sanitize_opaque_header("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert sanitize_opaque_header("   \t\n") is None

    def test_alphanumeric_passes_through(self) -> None:
        assert sanitize_opaque_header("abc123") == "abc123"

    def test_uuid_passes_through(self) -> None:
        assert (
            sanitize_opaque_header("4b3f1c8e-0d2a-4f3e-9c1b-7a5b8e2d4f6a")
            == "4b3f1c8e-0d2a-4f3e-9c1b-7a5b8e2d4f6a"
        )

    def test_dotted_passes_through(self) -> None:
        """Versioned ids like 'kiln.untrusted-content.v1' (PR #305)
        survive the sanitizer."""
        assert (
            sanitize_opaque_header("kiln.untrusted-content.v1")
            == "kiln.untrusted-content.v1"
        )

    def test_underscores_pass_through(self) -> None:
        assert sanitize_opaque_header("my_pipeline_42") == "my_pipeline_42"

    def test_truncates_at_200_chars(self) -> None:
        """The 200-char cap matches the OpenTelemetry W3C trace-id
        family (32 hex) plus vendor prefixes, and is well under
        Stripe's 255-char Idempotency-Key cap."""
        long = "a" * 300
        out = sanitize_opaque_header(long)
        assert out is not None
        assert len(out) == 200

    def test_truncate_then_scrub(self) -> None:
        """Truncation happens BEFORE scrubbing — a 300-char string
        with illegal chars only in the dropped tail still scrubs
        only the within-cap portion."""
        # First 200 chars are clean; chars 201-300 are spaces.
        s = ("a" * 200) + (" " * 100)
        out = sanitize_opaque_header(s)
        assert out == "a" * 200

    def test_newline_injected_value_scrubbed(self) -> None:
        """Belt-and-braces for log-injection / DB-pollution.
        Newlines, CRs, tabs, semicolons, slashes, quotes — anything
        outside [A-Za-z0-9._-] becomes underscore."""
        assert sanitize_opaque_header("foo\nbar") == "foo_bar"
        assert sanitize_opaque_header("foo\r\nbar") == "foo__bar"
        assert sanitize_opaque_header("a;b") == "a_b"
        assert sanitize_opaque_header("a/b") == "a_b"
        assert sanitize_opaque_header('a"b') == "a_b"

    def test_only_illegal_chars_scrubs_to_underscores(self) -> None:
        """Per the spec: the result is the scrubbed form, even when
        every char is illegal. (Not None.) The caller can decide
        what to do with an all-underscore key — but it's not a
        false-empty signal."""
        out = sanitize_opaque_header("///")
        assert out == "___"

    def test_leading_trailing_whitespace_trimmed(self) -> None:
        assert sanitize_opaque_header("  foo  ") == "foo"

    def test_stable_across_calls(self) -> None:
        """Sanitization is a pure function. Same input → same output."""
        v = "Pipeline/42!@#"
        assert sanitize_opaque_header(v) == sanitize_opaque_header(v)

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Idempotency-Key-1", "Idempotency-Key-1"),
            ("req_abc.123", "req_abc.123"),
            ("foo bar", "foo_bar"),
            (" \t leading-trim", "leading-trim"),
        ],
    )
    def test_table(self, value: str, expected: str) -> None:
        assert sanitize_opaque_header(value) == expected
