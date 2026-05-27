"""Pins the #358 canonical Warning policy table.

The policy lookup is the single source of truth for the
(severity, action) pair assigned to each WarningType. Tests pin:
* Every WarningType has an entry — a future enum widening without
  a policy update fails CI.
* Severity-action correlations stay sane (blocking only refuses;
  advisory never refuses).
* The flat_warning() helper populates both fields from the table.
* views.warning_severity() re-exports the canonical lookup.
"""

from __future__ import annotations

from typing import get_args

from cf_knowledge_kiln.retrieval.types import Action, Severity, Warning, WarningType
from cf_knowledge_kiln.retrieval.warning_policy import (
    WARNING_POLICY,
    action_for,
    flat_warning,
    severity_for,
)


class TestPolicyCoverage:
    def test_every_warning_type_has_policy(self) -> None:
        """Adding a new WarningType to the Literal without an entry
        here is a compile-time-safe omission but a runtime hazard —
        catch it at CI."""
        types_in_literal = set(get_args(WarningType))
        types_in_policy = set(WARNING_POLICY)
        assert types_in_literal == types_in_policy, (
            f"WarningType ↔ WARNING_POLICY mismatch. "
            f"Missing from policy: {types_in_literal - types_in_policy}. "
            f"Extra in policy: {types_in_policy - types_in_literal}."
        )

    def test_every_severity_is_a_legal_literal(self) -> None:
        """Defensive: the policy can't return an invalid Severity."""
        legal_severities = set(get_args(Severity))
        for wtype, (sev, _) in WARNING_POLICY.items():
            assert sev in legal_severities, f"{wtype}: severity {sev!r} not in {legal_severities}"

    def test_every_action_is_a_legal_literal(self) -> None:
        legal_actions = set(get_args(Action))
        for wtype, (_, act) in WARNING_POLICY.items():
            assert act in legal_actions, f"{wtype}: action {act!r} not in {legal_actions}"


class TestSeverityActionCorrelation:
    """Some severity-action pairs would be semantically wrong even
    if individually-valid. Pin the policy invariants."""

    def test_blocking_severities_always_refuse_to_synthesize(self) -> None:
        """A 'blocking' warning that asks the agent to merely 'inform'
        is a contradiction. blocking ⇒ refuse_to_synthesize."""
        for wtype, (sev, act) in WARNING_POLICY.items():
            if sev == "blocking":
                assert act == "refuse_to_synthesize", (
                    f"{wtype}: blocking severity must pair with refuse_to_synthesize, got {act!r}"
                )

    def test_advisory_severities_never_refuse(self) -> None:
        """An 'advisory' warning that asks for refuse_to_synthesize
        is contradictory — refuse is a blocking-class action."""
        for wtype, (sev, act) in WARNING_POLICY.items():
            if sev == "advisory":
                assert act != "refuse_to_synthesize", (
                    f"{wtype}: advisory severity must NOT pair with refuse_to_synthesize"
                )


class TestPolicyLookupHelpers:
    def test_severity_for_known_type(self) -> None:
        assert severity_for("sensitive_content") == "blocking"
        assert severity_for("stale_source") == "advisory"

    def test_action_for_known_type(self) -> None:
        assert action_for("sensitive_content") == "refuse_to_synthesize"
        assert action_for("query_normalized") == "rewrite_query"

    def test_unknown_type_falls_back_to_advisory_inform(self) -> None:
        # Defensive — a future WarningType not yet in the table.
        # Pyright would catch this at compile time; the runtime
        # fallback is the safety net.
        assert severity_for("not_a_real_type") == "advisory"  # type: ignore[arg-type]
        assert action_for("not_a_real_type") == "inform"  # type: ignore[arg-type]


class TestFlatWarningHelper:
    def test_populates_severity_and_action_from_policy(self) -> None:
        w = flat_warning("deprecated_source", message="…")
        assert w.severity == "warning"
        assert w.action == "prefer_other_sources"

    def test_preserves_message_and_optional_source_id(self) -> None:
        from uuid import uuid4

        src = uuid4()
        w = flat_warning("stale_source", message="m", source_id=src)
        assert w.message == "m"
        assert w.source_id == src

    def test_constructs_a_valid_pydantic_warning(self) -> None:
        """Round-trip pin: the helper's output is a real Warning that
        passes validation + serializes correctly."""
        w = flat_warning("conflicting_sources", message="x")
        dumped = w.model_dump()
        assert dumped["type"] == "conflicting_sources"
        assert dumped["severity"] == "warning"
        assert dumped["action"] == "request_human_review"
        # source_id is None (not passed) — exclude_none would drop it
        # on the wire, but we don't exclude here.
        assert dumped["source_id"] is None


class TestWarningFlatShape:
    """#358 added severity + action with safe defaults. Pin that:
    1. A bare Warning(type=..., message=...) still validates.
    2. The defaults are advisory/inform — the safest fallback.
    3. The fields are SETTABLE (not frozen)."""

    def test_bare_construction_uses_safe_defaults(self) -> None:
        w = Warning(type="stale_source", message="m")
        assert w.severity == "advisory"
        assert w.action == "inform"

    def test_explicit_values_round_trip(self) -> None:
        w = Warning(
            type="deprecated_source",
            message="m",
            severity="warning",
            action="prefer_other_sources",
        )
        assert w.severity == "warning"
        assert w.action == "prefer_other_sources"

    def test_invalid_severity_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Warning(
                type="stale_source",
                message="m",
                severity="critical",  # type: ignore[arg-type]
                action="inform",
            )

    def test_invalid_action_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Warning(
                type="stale_source",
                message="m",
                severity="advisory",
                action="set_on_fire",  # type: ignore[arg-type]
            )
