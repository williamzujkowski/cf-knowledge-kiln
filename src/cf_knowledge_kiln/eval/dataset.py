"""Golden-set loader for the retrieval eval harness (issue #31).

Golden cases are authored by ``(repo, path, heading_path)`` — the only
tuple that is stable across reingest. Chunk IDs change every time the
chunker runs; content excerpts change when the parser tightens.
``(repo, path, heading_path)`` is derived directly from the file
structure and survives both.

Two strict-equality matching modes:

* ``heading_path: [...]`` — the chunk's exact heading path must match.
* ``heading_path: []`` — match any chunk in the document. This is the
  "the document, anywhere" mode for queries where heading granularity
  isn't load-bearing for the case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cf_knowledge_kiln.retrieval import RetrievalFilters

SUPPORTED_VERSIONS = {1}


@dataclass(frozen=True)
class ExpectedHit:
    """A single expected (repo, path, heading_path) tuple per case."""

    repo: str
    path: str
    heading_path: list[str] = field(default_factory=list)
    must_appear_within_k: int = 10


@dataclass(frozen=True)
class GoldenCase:
    """One eval case: a query, optional filters, ordered expected hits."""

    case_id: str
    query: str
    filters: dict[str, Any]
    expected: list[ExpectedHit]
    notes: str | None = None


class GoldenSetError(ValueError):
    """Raised on malformed or duplicated golden-set entries."""


def load_golden_set(path: Path) -> list[GoldenCase]:
    """Parse + validate a golden YAML. Raises on duplicate case_ids."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "cases" not in raw:
        raise GoldenSetError(f"{path}: top-level 'cases' key missing")
    version = raw.get("version", 1)
    if version not in SUPPORTED_VERSIONS:
        raise GoldenSetError(
            f"{path}: unsupported schema version {version!r}; "
            f"this loader handles {sorted(SUPPORTED_VERSIONS)}"
        )
    cases_raw = raw["cases"]
    if not isinstance(cases_raw, list) or not cases_raw:
        raise GoldenSetError(f"{path}: 'cases' must be a non-empty list")

    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for idx, entry in enumerate(cases_raw):
        if not isinstance(entry, dict):
            raise GoldenSetError(f"{path}: case {idx} is not a mapping")
        case = _parse_case(entry, path, idx)
        if case.case_id in seen:
            raise GoldenSetError(f"{path}: duplicate case_id '{case.case_id}'")
        seen.add(case.case_id)
        cases.append(case)
    return cases


def _parse_case(entry: dict[str, Any], path: Path, idx: int) -> GoldenCase:
    for required in ("case_id", "query", "expected"):
        if required not in entry:
            raise GoldenSetError(f"{path}: case {idx} missing '{required}'")
    expected_raw = entry["expected"]
    if not isinstance(expected_raw, list) or not expected_raw:
        raise GoldenSetError(
            f"{path}: case '{entry['case_id']}' must have at least one expected hit"
        )
    expected = [_parse_hit(h, entry["case_id"], i) for i, h in enumerate(expected_raw)]
    filters_raw = entry.get("filters") or {}
    if not isinstance(filters_raw, dict):
        raise GoldenSetError(f"{path}: case '{entry['case_id']}' filters must be a mapping")
    # Fail fast at load-time so authors get a case-attributed message
    # rather than a Pydantic trace deep inside the runner.
    try:
        RetrievalFilters(**filters_raw)
    except Exception as exc:
        raise GoldenSetError(
            f"{path}: case '{entry['case_id']}' has invalid filters: {exc}"
        ) from exc
    return GoldenCase(
        case_id=str(entry["case_id"]),
        query=str(entry["query"]),
        filters=filters_raw,
        expected=expected,
        notes=entry.get("notes"),
    )


def _parse_hit(entry: object, case_id: str, idx: int) -> ExpectedHit:
    if not isinstance(entry, dict):
        raise GoldenSetError(f"case '{case_id}' expected[{idx}] is not a mapping")
    for required in ("repo", "path"):
        if required not in entry:
            raise GoldenSetError(f"case '{case_id}' expected[{idx}] missing '{required}'")
    heading = entry.get("heading_path", [])
    if not isinstance(heading, list) or not all(isinstance(h, str) for h in heading):
        raise GoldenSetError(
            f"case '{case_id}' expected[{idx}] heading_path must be a list of strings"
        )
    must_within = entry.get("must_appear_within_k", 10)
    if not isinstance(must_within, int) or must_within <= 0:
        raise GoldenSetError(
            f"case '{case_id}' expected[{idx}] must_appear_within_k must be a positive int"
        )
    return ExpectedHit(
        repo=str(entry["repo"]),
        path=str(entry["path"]),
        heading_path=list(heading),
        must_appear_within_k=must_within,
    )


# ─── Review-precision set (#108) ────────────────────────────────────

# The closed enum of expected_reason values. Author labels stay
# narrative; the runner uses this set to validate spelling at load
# time so a typo doesn't masquerade as a wrong-reason failure.
REVIEW_REASONS: frozenset[str] = frozenset(
    {
        "conflicting_sources",
        "deprecated_source",
        "sensitive_content",
        "prompt_injection_pattern",
        "weak_evidence",
        "empty_result",
        "none",
    }
)


@dataclass(frozen=True)
class ReviewCase:
    """One review-precision case: query + label + optional reason.

    The scorer asserts ``pack.requires_human_review == expected_review``
    per case and aggregates fraction-correct across the set. The
    optional ``expected_reason`` is narrative (it's not checked against
    the actual warning types fired) — kept so a future tightening of
    the test can add per-reason precision strata without a YAML rewrite.
    """

    case_id: str
    query: str
    filters: dict[str, Any]
    expected_review: bool
    expected_reason: str | None = None
    notes: str | None = None


def load_review_set(path: Path) -> list[ReviewCase]:
    """Parse + validate a review-precision YAML.

    Raises :class:`GoldenSetError` on duplicate case_ids, missing
    required keys, or invalid filter shapes — all the same failure
    modes the retrieval-quality loader catches, scaled to the
    review-precision case shape.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "cases" not in raw:
        raise GoldenSetError(f"{path}: top-level 'cases' key missing")
    version = raw.get("version", 1)
    if version not in SUPPORTED_VERSIONS:
        raise GoldenSetError(
            f"{path}: unsupported schema version {version!r}; "
            f"this loader handles {sorted(SUPPORTED_VERSIONS)}"
        )
    cases_raw = raw["cases"]
    if not isinstance(cases_raw, list) or not cases_raw:
        raise GoldenSetError(f"{path}: 'cases' must be a non-empty list")

    cases: list[ReviewCase] = []
    seen: set[str] = set()
    for idx, entry in enumerate(cases_raw):
        if not isinstance(entry, dict):
            raise GoldenSetError(f"{path}: case {idx} is not a mapping")
        case = _parse_review_case(entry, path, idx)
        if case.case_id in seen:
            raise GoldenSetError(f"{path}: duplicate case_id '{case.case_id}'")
        seen.add(case.case_id)
        cases.append(case)
    return cases


def _parse_review_case(entry: dict[str, Any], path: Path, idx: int) -> ReviewCase:
    for required in ("case_id", "query", "expected_review"):
        if required not in entry:
            raise GoldenSetError(f"{path}: case {idx} missing '{required}'")
    if not isinstance(entry["expected_review"], bool):
        raise GoldenSetError(f"{path}: case '{entry['case_id']}' expected_review must be a bool")
    filters_raw = entry.get("filters") or {}
    if not isinstance(filters_raw, dict):
        raise GoldenSetError(f"{path}: case '{entry['case_id']}' filters must be a mapping")
    try:
        RetrievalFilters(**filters_raw)
    except Exception as exc:
        raise GoldenSetError(
            f"{path}: case '{entry['case_id']}' has invalid filters: {exc}"
        ) from exc
    reason = entry.get("expected_reason")
    if reason is not None and (not isinstance(reason, str) or reason not in REVIEW_REASONS):
        raise GoldenSetError(
            f"{path}: case '{entry['case_id']}' expected_reason "
            f"{reason!r} not in {sorted(REVIEW_REASONS)}"
        )
    return ReviewCase(
        case_id=str(entry["case_id"]),
        query=str(entry["query"]),
        filters=filters_raw,
        expected_review=bool(entry["expected_review"]),
        expected_reason=reason,
        notes=entry.get("notes"),
    )


__all__ = [
    "REVIEW_REASONS",
    "ExpectedHit",
    "GoldenCase",
    "GoldenSetError",
    "ReviewCase",
    "load_golden_set",
    "load_review_set",
]
