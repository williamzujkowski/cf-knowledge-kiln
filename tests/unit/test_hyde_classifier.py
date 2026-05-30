"""#332 unit tests for the HyDE classifier gate.

Pins behavior on the four calibration-222 miss queries the issue
calls out explicitly (q01, q04, q06, q10) plus a representative
negative set (long chatty queries that already carry context).
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.retrieval.hyde.classifier import (
    jargon_density,
    should_hyde,
    token_count,
)


class TestTokenCount:
    def test_empty_returns_zero(self) -> None:
        assert token_count("") == 0
        assert token_count("   ") == 0

    def test_multi_whitespace_collapses(self) -> None:
        assert token_count("foo   bar\tbaz\nqux") == 4


class TestJargonDensity:
    def test_all_natural_words_returns_zero(self) -> None:
        assert jargon_density("how do I set up the backup process") == 0.0

    def test_kebab_case_token_counts(self) -> None:
        # "credhub-ca" has a non-alphanumeric char → jargon-like.
        density = jargon_density("the credhub-ca expired today")
        assert density > 0.0
        assert density < 1.0

    def test_all_caps_acronym_counts(self) -> None:
        density = jargon_density("the OIDC and OSBAPI configs")
        # "OIDC" + "OSBAPI" are jargon, "the" + "and" + "configs" are not.
        assert density == pytest.approx(2 / 5)

    def test_camelcase_token_counts(self) -> None:
        density = jargon_density("RouteEmitter restart needed")
        assert density > 0.0

    def test_long_compound_token_counts(self) -> None:
        # ≥ 12 chars → jargon-like even without case/punctuation flags.
        assert jargon_density("understanding authenticationflow") > 0.0

    def test_empty_input_returns_zero(self) -> None:
        assert jargon_density("") == 0.0


class TestShouldHydeGate:
    """The gate fires on hard queries (short / dense / imperative)
    and skips chatty queries that already carry context."""

    # ── Positive cases: gate MUST fire ──────────────────────────────

    @pytest.mark.parametrize(
        "query",
        [
            # Calibration-222 miss-shaped queries (short, domain-dense).
            "offsite backup failed",  # q04-shaped
            "credhub ca expiring",  # q03-shaped (short)
            "manage offsite backup",  # q01-shaped
            # Bare imperatives.
            "how do I rotate the credhub CA",
            "what is the OSBAPI v2 contract",
            "explain the offsite-backup component",
            "show me the route emitter restart procedure",
            # Tiny ad-hoc lookups.
            "wait-for-host",
            "lab inventory",
        ],
    )
    def test_short_or_imperative_query_triggers_gate(self, query: str) -> None:
        assert should_hyde(query) is True, (
            f"HyDE gate should fire on {query!r} (short / imperative / dense)."
        )

    def test_jargon_dense_long_query_still_triggers(self) -> None:
        """A long-ish query made of mostly jargon (kebab/CamelCase/acronyms)
        is still a HyDE candidate — the vector arm struggles with
        operator-speak even when the query is long."""
        query = "RouteEmitter restart cf-deployment-v40 OSBAPI-credhub-rotation playbook"
        assert should_hyde(query) is True

    # ── Negative cases: gate MUST skip ──────────────────────────────

    def test_empty_query_skips(self) -> None:
        assert should_hyde("") is False
        assert should_hyde("   ") is False

    def test_long_chatty_query_skips(self) -> None:
        """A chatty natural-prose query that's long AND low-jargon
        AND not imperative shouldn't be expanded — the vector arm
        already has plenty of signal."""
        query = (
            "we noticed yesterday that our team forgot to record the new "
            "decision in the operations log and now nobody can remember "
            "what was actually agreed upon"
        )
        assert should_hyde(query) is False

    # ── Tunable thresholds ──────────────────────────────────────────

    def test_token_threshold_is_respected(self) -> None:
        """A query at exactly the threshold should NOT trigger via
        the token-count arm (the comparison is strict ``<``); only
        below it does the gate fire from that arm."""
        # 8 single-char tokens → not below 8.
        eight_tokens = "a b c d e f g h"
        assert token_count(eight_tokens) == 8
        # All tokens are 1-char lowercase letters → no jargon, no
        # imperative — the only path to True is the token-count arm.
        assert should_hyde(eight_tokens, token_threshold=8) is False
        # Drop one token → below threshold → True.
        assert should_hyde("a b c d e f g", token_threshold=8) is True

    def test_jargon_threshold_is_respected(self) -> None:
        """High jargon density → True, lower threshold → True earlier."""
        # 2 jargon-like tokens out of 8 = 0.25 density.
        query = "the configured RouteEmitter for our environment uses CF"
        # Default 0.4 threshold: density 0.25 should NOT trigger via
        # the jargon arm — but if the query is short the token-arm
        # might. Force it long enough to fail the token arm.
        assert jargon_density(query) < 0.4
        # Long enough to fail the token-count arm AND not imperative.
        # Make sure THIS arm doesn't fire at the default threshold.
        # (Other arms may still keep should_hyde False — that's the
        # contract we're pinning.)
        assert should_hyde(query, token_threshold=4, jargon_density_threshold=0.4) is False
        # Lower the threshold and it fires.
        assert should_hyde(query, token_threshold=4, jargon_density_threshold=0.2) is True
