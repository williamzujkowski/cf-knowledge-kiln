"""Journey-level metrics for the end-to-end eval harness (#68).

The retrieval eval at :mod:`cf_knowledge_kiln.eval.scoring` answers
\"are the right chunks ranked high enough?\". This module answers a
different set of questions:

* **Citation presence** — does every returned result carry a
  citation (a `(repo, path)` tuple)? An uncited result is a bug per
  AGENTS.md's cited-or-silent rule.
* **Latency** — p50 / p95 / p99 across a query bank.
* **Warning emission** — for a case that *should* emit a particular
  warning (deprecation, prompt-injection, conflict), did the
  retrieval surface actually emit it?
* **Agent contract** — does the context pack respect its token
  budget, always include the untrusted-content notice, and exclude
  sensitive-content chunks from the body?

All functions are pure and operate on already-computed responses, so
they can be unit-tested without a DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class LatencyMetrics:
    """Latency percentiles over a sample of per-query durations."""

    p50: float
    p95: float
    p99: float
    samples: int


def latency_metrics(durations_seconds: list[float]) -> LatencyMetrics:
    """Compute p50 / p95 / p99 in seconds.

    Empty input yields zeroes — a zero-sample run is a degenerate
    edge case (the runner should reject it before calling), but
    returning all-zero is safer than raising mid-report.
    """
    if not durations_seconds:
        return LatencyMetrics(p50=0.0, p95=0.0, p99=0.0, samples=0)
    sorted_ds = sorted(durations_seconds)
    return LatencyMetrics(
        p50=_percentile(sorted_ds, 0.50),
        p95=_percentile(sorted_ds, 0.95),
        p99=_percentile(sorted_ds, 0.99),
        samples=len(sorted_ds),
    )


def _percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile. ``sorted_values`` must be ascending."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    # nearest-rank: index = ceil(p * n) - 1, clamped.
    idx = max(0, min(n - 1, int(p * n + 0.999999) - 1))
    return float(sorted_values[idx])


# ─── Citation presence ──────────────────────────────────────────────


def citation_presence_rate(results: list[Any]) -> float:
    """Fraction of results that carry both ``repo`` and ``path``.

    Anything missing either field is treated as uncited. AGENTS.md's
    cited-or-silent rule says the value should be 1.0 — anything
    below that is a regression.
    """
    if not results:
        return 0.0
    cited = 0
    for r in results:
        repo = _get(r, "repo")
        path = _get(r, "path")
        if repo and path:
            cited += 1
    return cited / len(results)


# ─── Warning emission ───────────────────────────────────────────────


def warning_kinds_in(response: Any) -> set[str]:
    """Extract the set of warning ``type`` strings from a response.

    The :class:`cf_knowledge_kiln.retrieval.types.Warning` model uses
    a ``type`` field (not ``kind``); the helper name says "kinds" for
    readability at call sites — they're the same concept.

    Handles both Pydantic models (``response.warnings``) and dicts
    (``response['warnings']``) — the test layer sometimes operates
    on the JSON shape, the engine layer on the model.
    """
    warnings = _get(response, "warnings") or []
    kinds: set[str] = set()
    for w in warnings:
        # Read both — `type` is the canonical field; `kind` is honored
        # so callers that hand us a normalized dict can use either.
        kind = _get(w, "type") or _get(w, "kind")
        if kind:
            kinds.add(str(kind))
    return kinds


def warning_emitted(response: Any, expected_kind: str) -> bool:
    """True if the response carries at least one warning of ``expected_kind``."""
    return expected_kind in warning_kinds_in(response)


# ─── Agent context-pack contract ────────────────────────────────────


def token_budget_respected(pack: Any) -> bool:
    """``used_estimate`` must never exceed ``requested``.

    A pack that overshoots is a contract violation — the agent caller
    sized its inbound buffer to ``requested`` and will truncate or
    crash on more.

    Defensive default: returns False on a missing ``token_budget`` or
    missing fields inside it. ``ContextPackResponse`` makes
    ``token_budget`` required, so for the engine path missing == schema
    violation == fail. Callers that need to distinguish "schema
    violated" from "budget breached" should inspect the pack
    themselves.
    """
    tb = _get(pack, "token_budget")
    if tb is None:
        return False
    used = _get(tb, "used_estimate")
    requested = _get(tb, "requested")
    if used is None or requested is None:
        return False
    return int(used) <= int(requested)


def untrusted_notice_present(pack: Any) -> bool:
    """The standard untrusted-content notice must always be set.

    The constant lives at
    :data:`cf_knowledge_kiln.agent.serializers.UNTRUSTED_CONTENT_NOTICE`;
    we don't pin the exact text here so a future wording change
    doesn't break this metric. Any non-empty string passes.
    """
    notice = _get(pack, "untrusted_content_notice")
    return bool(notice and isinstance(notice, str) and notice.strip())


def sensitive_chunks_excluded(pack: Any, sensitive_doc_ids: set[UUID]) -> bool:
    """No evidence chunk's ``document_id`` may be in ``sensitive_doc_ids``.

    Sensitive content is allowed to surface in human results with a
    redaction notice; agent context packs must drop it entirely.

    Shape-symmetric: a Pydantic ``EvidenceChunk`` carries
    ``document_id`` as a :class:`UUID`, while ``pack.model_dump(mode='json')``
    serializes it to a string. Both sides are coerced to ``str`` before
    membership so a JSON-shaped pack and a model-shaped pack get the
    same answer.
    """
    needle = {str(d) for d in sensitive_doc_ids}
    evidence = _get(pack, "evidence") or []
    for chunk in evidence:
        doc_id = _get(chunk, "document_id")
        if doc_id is not None and str(doc_id) in needle:
            return False
    return True


# ─── Helpers ────────────────────────────────────────────────────────


def _get(obj: Any, attr: str) -> Any:
    """Read ``attr`` from a Pydantic model OR a dict — caller-agnostic."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


__all__ = [
    "LatencyMetrics",
    "citation_presence_rate",
    "latency_metrics",
    "sensitive_chunks_excluded",
    "token_budget_respected",
    "untrusted_notice_present",
    "warning_emitted",
    "warning_kinds_in",
]
