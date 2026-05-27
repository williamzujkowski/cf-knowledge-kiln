"""Pins the #325 fix: each OIDC callback rejection path logs WARNING
with a structured ``reason`` + ``request_id`` extra.

Prior shape: most paths logged at INFO (below the default operator
filter); the required-group rejection logged NOTHING. Operators had
to instrument the middleware to find out which of the 5 paths
tripped after a complaint.

New shape: every rejection branch in ``_handle_callback`` logs at
WARNING with ``extra={"reason": <reason>, "request_id": <id>, ...}``
so a log aggregator's structured-fields filter picks it up.

The wire response stays the generic ``auth_required`` envelope —
the differentiation is server-side only (don't leak to attackers).

Test strategy: source-grep the middleware module for each
``reason`` value. This is more durable than mocking the full
callback flow (which needs httpx + JWKS + a state cookie) and
catches the exact contract the issue cares about.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_AUTH_PY = _REPO / "src/cf_knowledge_kiln/api/auth.py"


def _source() -> str:
    return _AUTH_PY.read_text()


class TestEachRejectionPathLogsWarning:
    """Every rejection branch must log at WARNING (not INFO, not bare)
    and carry a structured ``reason`` field so an operator can grep
    one event class."""

    REASONS = (
        "state_cookie_missing",
        "state_cookie_expired",
        "state_cookie_bad_signature",
        "state_mismatch",
        "code_missing",
        "discovery_no_token_endpoint",
        "token_exchange_failed",
        "id_token_missing",
        "id_token_invalid",
        "required_group_missing",
    )

    def test_each_reason_present_as_log_extra(self) -> None:
        """Every documented rejection branch carries a unique
        ``reason`` value in its ``extra={...}`` block. A future
        refactor that drops one is caught here."""
        source = _source()
        for reason in self.REASONS:
            needle = f'"reason": "{reason}"'
            assert needle in source, (
                f"Missing structured reason in oidc callback: {reason!r}. "
                f"Every rejection branch must log with extra={{'reason': ...}} "
                f"so operators can grep one event class."
            )

    def test_no_info_level_log_for_rejection_branches(self) -> None:
        """The prior INFO-level rejection logs masked the events from
        operators (most aggregators default-filter INFO). Pin that the
        regressed shape doesn't return."""
        source = _source()
        # Anchor on the strings that were INFO; if they reappear,
        # the regression is back. The new shape uses
        # ``rejected — <prose>`` instead.
        forbidden_patterns = [
            'logger.info("auth: oidc state mismatch")',
            'logger.info("auth: oidc callback missing code")',
            'logger.info("auth: oidc id_token rejected:',
        ]
        for pat in forbidden_patterns:
            assert pat not in source, (
                f"Regression — INFO-level rejection log reappeared: {pat!r}. "
                f"Bump to logger.warning(... extra={{'reason': ..., 'request_id': ...}})"
            )


class TestRequestIdCorrelation:
    """Structured ``request_id`` lets an operator cross-reference
    the warning with the per-request log line and the wire-level
    request_id the user sees in the auth_required envelope."""

    def test_each_reason_carries_request_id(self) -> None:
        """Walk the source: every ``extra=`` dict that names a
        rejection reason MUST also carry request_id."""
        source = _source()
        # Locate each "reason": "<name>" occurrence and check the
        # ~200-char block around it carries "request_id":.
        for reason in TestEachRejectionPathLogsWarning.REASONS:
            needle = f'"reason": "{reason}"'
            idx = source.index(needle)
            window = source[max(idx - 50, 0) : idx + 250]
            assert '"request_id"' in window, (
                f"reason={reason!r} log call must carry 'request_id' "
                f"in its extra dict for log-aggregator correlation."
            )


class TestWireShapeUnchanged:
    """Issue #325 explicitly: server-side log verbosity changes; the
    wire response stays the generic auth_required envelope. Pin
    that we still call _unauthorized() / _forbidden() — not a more
    specific error code that would leak which path tripped."""

    def test_unauthorized_still_used_for_state_rejections(self) -> None:
        source = _source()
        # Every state-cookie / state-mismatch rejection returns
        # _unauthorized(...) — that's the don't-leak shape.
        # Just count occurrences as a sanity check; the precise
        # branching is covered by the integration tests in
        # test_oidc_middleware.py.
        assert source.count("_unauthorized(req_id)") >= 5

    def test_forbidden_still_used_for_required_group(self) -> None:
        source = _source()
        # Required-group denial → _forbidden (403 vs 401 — the
        # operator can tell from the status code, the wire body
        # still says auth_required).
        assert "_forbidden(" in source
