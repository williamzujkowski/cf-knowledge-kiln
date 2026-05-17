"""End-to-end UX evaluation for the human + agent journeys (#68).

Complements :mod:`tests.eval.test_golden` (which scores recall@K and
MRR for the retrieval engine) with journey-level metrics:

* **Human** (`/v1/search` path): citation-presence rate on every
  result; latency p50/p95/p99 over a query bank; deprecation
  warning surfaces on a query that targets a deprecated doc.
* **Agent** (`/v1/agent/context-pack` path): token budget never
  exceeded; untrusted-content notice always present; sensitive
  chunks (none in this corpus today) excluded; prompt-injection
  fixture surfaces the warning and trips ``requires_human_review``.

The fixtures live at ``tests/eval/fixtures/adversarial/`` and the
``adversarial_retriever`` conftest fixture ingests them alongside the
real ``docs/`` corpus.

The metrics here run alongside the retrieval-quality harness; both
together close issue #68. Thresholds are bootstrap values — raise
them in follow-up PRs once a real embedding provider runs.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cf_knowledge_kiln.eval import (
    citation_presence_rate,
    latency_metrics,
    token_budget_respected,
    untrusted_notice_present,
    warning_kinds_in,
)
from cf_knowledge_kiln.retrieval import HybridRetriever, RetrievalFilters

pytestmark = [pytest.mark.integration, pytest.mark.eval]


# Bootstrap thresholds. Tighten as the harness matures.
LATENCY_P95_CEILING_S = 5.0  # generous; MockEmbedding + small corpus
CITATION_RATE_FLOOR = 1.0  # AGENTS.md cited-or-silent — must be 1.0


_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPORTS_DIR = _REPO_ROOT / "tests" / "eval" / "reports"


# A small query bank for the latency + citation sweeps. Drawn from
# the real docs/ corpus the conftest seeds.
_QUERIES = [
    "four-layer architecture experience retrieval index ingestion",
    "untrusted input handling indexed content markers",
    "KILN environment variables configuration precedence",
    "Cloud Foundry deployment manifest cf push two apps",
    "ingestion source allowlist configuration repo path",
]


def _empty_filters() -> RetrievalFilters:
    return RetrievalFilters()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─── Human journey: /v1/search shape ────────────────────────────────


def test_human_journey_every_result_is_cited(seeded_retriever: HybridRetriever) -> None:
    """AGENTS.md cited-or-silent rule: every result must carry repo + path."""

    async def _sweep() -> list[float]:
        rates: list[float] = []
        for q in _QUERIES:
            result = await seeded_retriever.search(q, filters=_empty_filters(), max_results=10)
            # Build the same "result card" shape the API layer uses —
            # citation_presence_rate operates on (repo, path) attrs
            # which exist on DocumentRef in result.document_refs.
            cards = [result.document_refs.get(c.document_id) for c in result.chunks]
            rates.append(citation_presence_rate(cards))
        return rates

    rates = _run(_sweep())
    assert rates, "expected at least one query to return results"
    # Every query that returned anything must have citation rate 1.0.
    for q, rate in zip(_QUERIES, rates, strict=True):
        assert rate == CITATION_RATE_FLOOR, (
            f"query {q!r}: citation rate {rate:.3f} below floor {CITATION_RATE_FLOOR:.3f}"
        )


def test_human_journey_latency_p95_under_ceiling(
    seeded_retriever: HybridRetriever,
) -> None:
    """p95 across the query bank must beat the bootstrap ceiling."""

    async def _measure() -> list[float]:
        durations: list[float] = []
        for q in _QUERIES:
            t0 = time.perf_counter()
            await seeded_retriever.search(q, filters=_empty_filters(), max_results=10)
            durations.append(time.perf_counter() - t0)
        return durations

    durations = _run(_measure())
    metrics = latency_metrics(durations)
    # Persist latency alongside the eval report so operators can
    # diff between runs.
    _persist_latency(metrics)
    assert metrics.p95 <= LATENCY_P95_CEILING_S, (
        f"latency p95 {metrics.p95:.2f}s exceeds ceiling {LATENCY_P95_CEILING_S:.2f}s "
        f"(p50={metrics.p50:.2f}s, p99={metrics.p99:.2f}s, n={metrics.samples})"
    )


# ─── Adversarial: deprecated docs surface deprecation warning ───────


def test_human_journey_deprecation_warning_on_deprecated_doc(
    adversarial_retriever: HybridRetriever,
) -> None:
    """Querying with status=[active, deprecated] must surface the deprecation warning.

    The default status filter penalty (status_weight=0.2 for
    ``deprecated``) pushes the lone deprecated fixture out of the top
    10 in the bootstrap corpus. The user journey we're modelling here
    is \"show me historical guidance, flag it\" — explicit opt-in via
    the status filter — which is the realistic shape for an operator
    asking about a deprecated runbook on purpose.
    """

    async def _go() -> Any:
        # Scope to the adversarial source so the bootstrap-corpus
        # status-weight downrank (0.2 for deprecated * 0.3 freshness
        # = 16x penalty vs active) can't push the lone deprecated
        # fixture out of the top 10. Real callers use the same
        # repo+status combo when asking about a specific tree's
        # historical guidance.
        return await adversarial_retriever.search(
            "thiamine-zero pipeline deprecated runbook",
            filters=RetrievalFilters(
                status=["active", "deprecated"],
                repo=["adversarial-fixtures"],
            ),
            max_results=10,
        )

    result = _run(_go())
    # The deprecation warning kind is emitted by the ranking layer
    # when a deprecated doc appears in the result set. We don't pin
    # the exact kind string — any warning carrying "deprecated" in
    # the kind counts.
    kinds = warning_kinds_in(result)
    assert any("deprecat" in k.lower() for k in kinds), (
        f"expected a deprecation warning, got kinds={kinds!r}"
    )


# ─── Agent journey: /v1/agent/context-pack contract ─────────────────


def test_agent_journey_untrusted_notice_always_present(
    seeded_retriever: HybridRetriever,
) -> None:
    """The standard untrusted-content notice must be set on every pack."""

    async def _go() -> Any:
        return await seeded_retriever.context_pack(
            query="four-layer architecture",
            task="summarize the architecture",
            filters=_empty_filters(),
            max_chunks=5,
            max_tokens=1500,
        )

    pack = _run(_go())
    assert untrusted_notice_present(pack), (
        f"context pack missing untrusted_content_notice: {pack!r}"
    )


def test_agent_journey_token_budget_respected(
    seeded_retriever: HybridRetriever,
) -> None:
    """``used_estimate`` must never exceed ``requested`` across the query bank."""

    async def _sweep() -> list[Any]:
        return [
            await seeded_retriever.context_pack(
                query=q,
                task=f"summarize results for: {q}",
                filters=_empty_filters(),
                max_chunks=5,
                max_tokens=1500,
            )
            for q in _QUERIES
        ]

    packs = _run(_sweep())
    breached = [p for p in packs if not token_budget_respected(p)]
    assert not breached, (
        f"{len(breached)} of {len(packs)} packs exceeded token budget: "
        f"{[(p.token_budget.used_estimate, p.token_budget.requested) for p in breached]}"
    )


def test_agent_journey_prompt_injection_emits_warning(
    adversarial_retriever: HybridRetriever,
) -> None:
    """Querying for the prompt-injection fixture must trip the warning + review flag."""

    async def _go() -> Any:
        return await adversarial_retriever.context_pack(
            query="honeycomb adversary prompt injection test",
            task="report on this content",
            filters=_empty_filters(),
            max_chunks=5,
            max_tokens=1500,
        )

    pack = _run(_go())
    # First: prove the prompt-injection chunk actually surfaced in
    # the evidence. If a future ranking change pushes it out, the
    # warning + requires_human_review assertions below would both
    # pass vacuously (no PI chunk → no warning → no review flag).
    # Make the failure mode diagnostically distinct.
    evidence_paths = [getattr(e, "path", None) for e in pack.evidence]
    assert any(p and "prompt-injection.md" in p for p in evidence_paths), (
        "prompt-injection fixture did not surface in evidence — the "
        "warning/review assertions below would pass vacuously. "
        f"evidence paths: {evidence_paths!r}"
    )
    # Warning must be present on the pack itself.
    kinds = warning_kinds_in(pack)
    assert any("prompt" in k.lower() and "inject" in k.lower() for k in kinds), (
        f"expected a prompt_injection warning, got kinds={kinds!r}"
    )
    # Independent assertion: requires_human_review must be True when
    # prompt-injection is in scope. The boolean flag is the
    # contract the agent caller actually keys on.
    assert pack.requires_human_review is True, (
        "expected requires_human_review=True for a pack containing a prompt-injection-tainted chunk"
    )


def test_agent_journey_warning_helper_handles_dict_form(
    seeded_retriever: HybridRetriever,
) -> None:
    """The warning helper must work on both Pydantic models and dicts.

    Regression guard: the journey-scoring helpers are called from
    pytest assertions on Pydantic models AND from the JSON report
    writers on dict-shaped responses. Both code paths must agree.
    """

    async def _go() -> Any:
        return await seeded_retriever.context_pack(
            query="anything",
            task="anything",
            filters=_empty_filters(),
            max_chunks=1,
            max_tokens=100,
        )

    pack = _run(_go())
    kinds_from_model = warning_kinds_in(pack)
    kinds_from_dict = warning_kinds_in(pack.model_dump(mode="json"))
    assert kinds_from_model == kinds_from_dict


# ─── Report side-effect ─────────────────────────────────────────────


def _persist_latency(metrics: Any) -> None:
    """Append latency metrics to the eval reports dir for operator diff."""
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORTS_DIR / "latest-latency.md"
    ts = datetime.now(UTC).isoformat()
    body = (
        "# Journey latency report\n\n"
        f"_Generated {ts}_\n\n"
        f"- **p50:** {metrics.p50:.3f} s\n"
        f"- **p95:** {metrics.p95:.3f} s\n"
        f"- **p99:** {metrics.p99:.3f} s\n"
        f"- **samples:** {metrics.samples}\n"
        f"- **ceiling (p95):** {LATENCY_P95_CEILING_S:.2f} s\n"
    )
    path.write_text(body, encoding="utf-8")


# ─── #100: extended adversarial coverage ────────────────────────────


def test_human_journey_staleness_warning_on_old_doc(
    adversarial_retriever: HybridRetriever,
) -> None:
    """#100: a doc with an old ``last_reviewed`` must emit stale_source.

    The fixture has ``last_reviewed: 2022-01-15`` — well past the
    default stale window — so the warning should surface whenever
    the chunk appears in a result set. Scope to the adversarial repo
    so the stale chunk reliably surfaces.
    """

    async def _go() -> Any:
        return await adversarial_retriever.search(
            "verbena-five rotation stale runbook",
            filters=RetrievalFilters(status=["active"], repo=["adversarial-fixtures"]),
            max_results=10,
        )

    result = _run(_go())
    kinds = warning_kinds_in(result)
    assert any("stale" in k.lower() for k in kinds), (
        f"expected a stale_source warning, got kinds={kinds!r}"
    )


def test_agent_journey_conflict_warning_on_same_heading(
    adversarial_retriever: HybridRetriever,
) -> None:
    """#100: two docs sharing a heading_path must trip the conflict warning.

    The /v1/agent/context-pack path runs ``detect_conflicts`` (syntactic
    same-heading across distinct active docs). The fixtures conflict-a.md
    and conflict-b.md both heading_path = ["Saxon-blue migration steps"].
    """

    async def _go() -> Any:
        return await adversarial_retriever.context_pack(
            query="saxon-blue migration steps",
            task="report the procedure",
            filters=RetrievalFilters(repo=["adversarial-fixtures"]),
            max_chunks=10,
            max_tokens=2000,
        )

    pack = _run(_go())
    # Conflict surfaces both in pack.conflicts AND as a warning of
    # type "conflicting_sources" appended in the engine.
    kinds = warning_kinds_in(pack)
    assert any("conflict" in k.lower() for k in kinds), (
        f"expected a conflicting_sources warning, got kinds={kinds!r}; "
        f"conflicts={[c.topic for c in pack.conflicts]!r}"
    )
    assert pack.conflicts, "expected at least one Conflict in pack.conflicts"


def test_human_journey_deprecation_warning_under_default_filter(
    adversarial_retriever: HybridRetriever,
) -> None:
    """#100: deprecation warning surfaces without an explicit status filter.

    The deprecated fixture (deprecated.md) is now boosted with repeated
    keywords + a recent last_reviewed so FTS dominates the
    status-weight penalty. Default filter (no status restriction)
    should still surface the deprecated chunk in the top 10, and the
    warning must fire.
    """

    async def _go() -> Any:
        return await adversarial_retriever.search(
            "thiamine-zero thiamine-zero pipeline deprecated runbook",
            filters=_empty_filters(),  # no status restriction
            max_results=10,
        )

    result = _run(_go())
    kinds = warning_kinds_in(result)
    assert any("deprecat" in k.lower() for k in kinds), (
        f"expected a deprecation warning under the default filter, got kinds={kinds!r}"
    )
