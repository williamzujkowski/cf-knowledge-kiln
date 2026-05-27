"""Canonical per-WarningType policy lookup (#358).

Centralises the (severity, action) mapping for every WarningType so
the engine, the UI views, the agent serializer, and the docs all
agree on what each warning *means* without each carrying its own
copy of the table.

Prior to #358 the policy lived implicitly in three places:

* ``_WARNING_SEVERITY`` in :mod:`cf_knowledge_kiln.api.views`
  (template-side severity bucket for the HTMX UI)
* Per-emitter prose in the variant classes' messages (the engine
  side)
* The "Suggested action" column in
  ``docs/agent-integration-guide.md`` §X (the consumer side)

Drift was the failure mode. This module is the single source of
truth; the others re-export from here.

#358 / Epic D (#329).
"""

from __future__ import annotations

from typing import Final

from cf_knowledge_kiln.retrieval.types import (
    Action,
    Severity,
    Warning,
    WarningType,
)

# The canonical policy table. EVERY WarningType MUST appear here;
# the test ``test_every_warning_type_has_policy`` enforces it.
#
# Severity rationale per row:
#   * ``stale_source`` / ``query_normalized`` / ``answer_truncated``
#     → advisory. Informational; the result is still usable as-is,
#     the operator just needs to know about the side condition.
#   * ``deprecated_source`` / ``weak_evidence`` / ``isolated_match``
#     / ``conflicting_sources`` → warning. The result is still useful
#     but the operator should weigh the signal before citing.
#   * ``prompt_injection_pattern`` / ``sensitive_content`` → blocking.
#     The chunk is dropped from agent context packs entirely; humans
#     see it with a refuse-to-cite stamp.
#
# Action rationale per row:
#   * ``inform`` — surface the warning, then proceed.
#   * ``prefer_other_sources`` — downweight; cite the alternative.
#   * ``request_human_review`` — defer (the answer pipeline already
#     sets requires_human_review).
#   * ``rewrite_query`` — surface the normalization to the user.
#   * ``refuse_to_synthesize`` — drop the chunk; refuse if it's the
#     sole evidence.
WARNING_POLICY: Final[dict[WarningType, tuple[Severity, Action]]] = {
    "stale_source": ("advisory", "inform"),
    "deprecated_source": ("warning", "prefer_other_sources"),
    "conflicting_sources": ("warning", "request_human_review"),
    "weak_evidence": ("warning", "request_human_review"),
    "isolated_match": ("warning", "request_human_review"),
    "prompt_injection_pattern": ("blocking", "refuse_to_synthesize"),
    "sensitive_content": ("blocking", "refuse_to_synthesize"),
    "query_normalized": ("advisory", "rewrite_query"),
    "answer_truncated": ("advisory", "inform"),
}


def severity_for(warning_type: WarningType) -> Severity:
    """Return the severity bucket for a Warning type.

    Falls back to ``"advisory"`` for any type missing from the table
    (defensive: a future WarningType added without a policy entry
    surfaces as an informational note rather than a 500). The test
    ``test_every_warning_type_has_policy`` catches the gap at CI time.
    """
    return WARNING_POLICY.get(warning_type, ("advisory", "inform"))[0]


def action_for(warning_type: WarningType) -> Action:
    """Return the recommended-action bucket for a Warning type.

    Same defensive fallback as :func:`severity_for`.
    """
    return WARNING_POLICY.get(warning_type, ("advisory", "inform"))[1]


def flat_warning(
    warning_type: WarningType,
    *,
    message: str,
    source_id: object | None = None,
) -> Warning:
    """Construct a flat :class:`Warning` with severity + action populated.

    This is the migration helper for emitters that don't yet construct
    a discriminated variant (most of ``ranking.py`` /
    ``_engine_helpers.py`` already does — see PR #375 — so this helper
    is mostly used by tests and by the few legacy call sites that
    haven't migrated yet).

    The variant-construction path (StaleSourceWarning(...).downgrade_to_flat())
    is preferred because it surfaces the per-variant fields too; this
    helper is the equivalent for callers that only need the flat shape.
    """
    severity, action = WARNING_POLICY.get(warning_type, ("advisory", "inform"))
    return Warning(
        type=warning_type,
        message=message,
        source_id=source_id,  # type: ignore[arg-type]
        severity=severity,
        action=action,
    )


__all__ = [
    "WARNING_POLICY",
    "action_for",
    "flat_warning",
    "severity_for",
]
