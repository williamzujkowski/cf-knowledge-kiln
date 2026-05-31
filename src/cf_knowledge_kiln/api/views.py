"""View-shaping helpers for the HTMX search results (#129).

Extracted from :mod:`cf_knowledge_kiln.api.web` so the route module
stays under the AGENTS.md 400-line soft cap. These helpers turn
engine outputs into shapes the Jinja templates consume — they don't
own routing or form parsing.

Public surface:

* :func:`humanize_warning` — engine :class:`Warning` to ``{prefix, message, type}``
* :func:`log_human_query` — best-effort ``rag_queries`` telemetry write
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

# #408 F17 staleness signal helpers live in api/freshness.py - extracted
# in the review-fix pass to keep views.py at the cap. Re-exported here
# (via the __all__ list) so the import path stays stable.
from cf_knowledge_kiln.api.freshness import freshness_bucket, freshness_label
from cf_knowledge_kiln.db.repositories import QueriesRepository
from cf_knowledge_kiln.retrieval import RetrievalFilters

logger = logging.getLogger(__name__)

# Spec-mandated warning copy from docs/user-journeys.md, plus a quiet
# italic prefix for the other emitted warning types. The template
# renders {prefix, message} and lets the prefix lead with italic
# voice instead of dropping the engine's raw string into the page.
_WARNING_COPY: dict[str, tuple[str, str]] = {
    "weak_evidence": (
        "Confidence is low —",
        "I found related content, but no clearly authoritative source.",
    ),
    "conflicting_sources": (
        "Sources disagree —",
        "I found multiple sources that may conflict. "
        "Prefer active/approved docs unless you are researching history.",
    ),
    "stale_source": ("Source is stale —", ""),
    "deprecated_source": ("Document is deprecated —", ""),
    "prompt_injection_pattern": ("Caution —", ""),
    "sensitive_content": ("Sensitive content —", ""),
    "query_normalized": ("Query was normalized —", ""),
}


def humanize_warning(w: Any) -> dict[str, Any]:
    """Map an engine :class:`Warning` to a template-ready dict.

    Returns ``{type, prefix, message, source_id, severity}``.

    Falls back to the engine's raw ``message`` when the warning type
    isn't in the spec-mandated copy table (so a future warning type
    surfaces something rather than nothing).

    An empty override string in :data:`_WARNING_COPY` is intentional:
    the prefix carries the spec-mandated voice, and the engine's raw
    message carries the per-instance detail (e.g., \"Document last
    reviewed 2024-01-15\"). The two together read as a margin note.

    ``source_id`` (#257) flows through so the route can route
    per-document warnings to inline per-card rendering; query-global
    warnings (those without a source_id) stay at the top of the list.
    ``severity`` (#257) is the visual-treatment bucket the template
    uses for inline badge styling.
    """
    wtype = getattr(w, "type", "")
    raw = getattr(w, "message", "") or ""
    prefix, override = _WARNING_COPY.get(wtype, ("", raw))
    source_id = getattr(w, "source_id", None)
    return {
        "type": wtype,
        "prefix": prefix,
        "message": override or raw,
        "source_id": str(source_id) if source_id is not None else None,
        "severity": warning_severity(wtype),
    }


def warning_severity(wtype: str) -> str:
    """Return the visual-severity bucket for ``wtype`` (#257 → #358).

    Wraps :func:`cf_knowledge_kiln.retrieval.warning_policy.severity_for`
    so the template-side lookup uses the single canonical policy table
    (was a duplicate dict here; the policy module is now the source of
    truth). Falls back to ``advisory`` for any unrecognized type so a
    future warning surfaces as a quiet hint until the UI catches up —
    not as a high-stakes red flag.
    """
    from cf_knowledge_kiln.retrieval.warning_policy import severity_for

    # severity_for takes a WarningType Literal; the call site passes
    # whatever Warning.type carries (which IS one of those values at
    # runtime). The cast is a typing-only narrowing.
    return severity_for(wtype)  # type: ignore[arg-type]


# Score-tier thresholds for the 5-dot visual scale (#259). Reflect the
# normalized fused-RRF scale (#164):
#   * both-arm rank-1 hit → 1.0   (tier 5)
#   * strong cross-arm hit → ~0.7 (tier 4)
#   * single-arm rank-1 hit → 0.5 (tier 3)
#   * at the default weak_evidence floor → 0.46 (tier 2)
#   * below the configured weak_evidence floor → tier 1 (only visible
#     when an operator tuned the floor down)
#
# Ordered low-to-high so the lookup walks bottom-up: any score
# below the lowest threshold falls to tier 1.
_SCORE_TIERS: tuple[tuple[float, int], ...] = (
    (0.85, 5),
    (0.65, 4),
    (0.50, 3),
    (0.46, 2),
)


def score_tier(score: float) -> int:
    """Map a fused-RRF score to a 1-5 visual tier (#259).

    Tier 5 = both-arm rank-1 quality; tier 1 = below the
    weak_evidence floor. Tier values drive a per-tier color +
    filled-dot count in the result-card score widget. See
    ``_SCORE_TIERS`` for the threshold table.
    """
    for threshold, tier in _SCORE_TIERS:
        if score >= threshold:
            return tier
    return 1


# Editorial stamp text per non-current status (#268). The stamp on
# a result card replaces the silent 'subtle stripe' signal with
# verbal copy a security engineer reads at scan speed:
#
#   deprecated  → "Deprecated · do not cite"
#   archived    → "Archived · historical reference"
#   superseded  → "Superseded · see successor"
#
# Voice is editorial — academic journal meets library catalog stamp.
# An ``active`` / ``approved`` / ``draft`` status has NO stamp (the
# absence of a stamp is the signal that the card is current).
_DEPRECATION_LABELS: dict[str, str] = {
    "deprecated": "Deprecated · do not cite",
    "archived": "Archived · historical reference",
    "superseded": "Superseded · see successor",
}


# Status-badge tooltips (#280). The badge is color-coded (teal /
# gold / oxblood) and a new or color-blind user has no legend for
# what each color means. The tooltip is the editorial gloss that
# rendsers into ``title`` for sighted-mouse hover AND into the
# ``aria-label`` combined with the visible status word.
#
# 'active' is glossed as 'Current — …' (not 'Active — …') to avoid
# the awkward 'Active — active.' redundancy. Every other status
# leads with the title-cased status word so the AT announcement
# reads naturally: 'deprecated: Deprecated — superseded; do not
# cite as current.'
#
# Corpus-native statuses outside this table (e.g. 'reference',
# 'canonical', 'running' — per the #203 open-status model) return
# None; the template skips the attribute rather than guessing a
# meaning the operator never wrote down.
_STATUS_TOOLTIPS: dict[str, str] = {
    "active": "Current — the canonical version.",
    "approved": "Approved — reviewed and signed off.",
    "draft": "Draft — not yet approved as authoritative.",
    "deprecated": "Deprecated — superseded; do not cite as current.",
    "archived": "Archived — kept for historical reference.",
    "superseded": "Superseded — see the linked successor.",
}


def status_tooltip(status: str) -> str | None:
    """Return the editorial gloss for a status, or None if unknown.

    Returns ``None`` for any status outside the kiln-recommended
    Literal vocabulary so the template can ``{% if r.status_tooltip
    %}`` without per-status guards. The badge still renders
    color-coded; just without the tooltip.
    """
    return _STATUS_TOOLTIPS.get(status)


def deprecation_label(status: str) -> str | None:
    """Return the editorial stamp text for a non-current status, or None.

    Spec: ``docs/user-journeys.md:55-57`` — 'Deprecated/archived/
    superseded results may appear but MUST be visually flagged.
    Showing a deprecated doc as if it were current is a bug, not a
    feature.' The verbal stamp is the strongest non-color signal we
    can ship; the stripe + strikethrough + gutter rule reinforce.

    Returns ``None`` for active / approved / draft so the template
    can `{% if r.deprecation_label %}` without a per-status switch.
    """
    return _DEPRECATION_LABELS.get(status)


# #336 — authority bucket. Documents in a corpus typically declare an
# authority via frontmatter: ``platform`` (the canonical owner team),
# ``security`` (the security team's hardening doc), ``standards``
# (org-wide standard), etc. Without surfacing this, a user scanning
# results can't tell which card carries the strongest backing.
#
# Editorial tooltip per known value so the AT user gets the same
# disambiguation a sighted user gets on hover/focus. Unknown values
# return None — the template skips the tooltip + the badge falls
# back to the raw string.
_AUTHORITY_TOOLTIPS: dict[str, str] = {
    "platform": "Platform — canonical owner-team doc.",
    "security": "Security — hardening or threat-model authoritative.",
    "standards": "Standards — org-wide policy.",
    "compliance": "Compliance — audit / regulatory backing.",
    "ops": "Ops — operational runbook authoritative.",
    "engineering": "Engineering — practice or pattern authoritative.",
    "operator": "Operator — team-curated, not org-wide canonical.",
    "community": "Community — peer-contributed, less authoritative.",
    "experimental": "Experimental — under evaluation.",
}


# #408 F18 — authority legend ordering. The visible legend lists
# authorities in descending-authority order so a new user reads the
# vocabulary top-down: canonical owner doc → standards-backed →
# team-curated → peer-contributed → under evaluation. The order
# isn't enforced in retrieval (the authority field isn't used as
# a boost weight today); it's purely a teaching aid.
_AUTHORITY_ORDER: tuple[str, ...] = (
    "platform",
    "security",
    "standards",
    "compliance",
    "ops",
    "engineering",
    "operator",
    "community",
    "experimental",
)


def authority_vocabulary() -> tuple[tuple[str, str], ...]:
    """Return ``((authority, tooltip), ...)`` for the legend (#408 F18).

    Tuple-of-tuples so the template can iterate in display order
    without re-sorting. Each entry: ``(short_name, sentence_tooltip)``.
    Pure function — no I/O, no global state outside this module.
    """
    return tuple((a, _AUTHORITY_TOOLTIPS[a]) for a in _AUTHORITY_ORDER)


# #408 F2 — score legend. Maps each tier to a one-word qualifier
# the user can scan. The tier vocabulary mirrors the score_tier()
# bucketing (5 = strong both-arm hit, 1 = below the weak-evidence
# floor). Pinned as a separate constant so a future renaming pass
# touches the legend AND the score-tier bucketing in one place.
_SCORE_TIER_LABELS: tuple[tuple[int, str], ...] = (
    (5, "strong both-arm match"),
    (4, "cross-arm match"),
    (3, "single-arm match"),
    (2, "at the weak-evidence floor"),
    (1, "below the weak-evidence floor"),
)


def score_legend_tiers() -> tuple[tuple[int, str], ...]:
    """Return ``((tier, label), ...)`` for the score legend (#408 F2).

    Listed strong → weak so the legend reads top-down as a quality
    ladder. The tier integers match :func:`score_tier` output, so
    the legend chip can render the same 5-dot widget the result-card
    score uses (only with the tier hard-coded).
    """
    return _SCORE_TIER_LABELS


def authority_tooltip(authority: str | None) -> str | None:
    """Return the editorial gloss for an authority value, or None.

    Same shape as :func:`status_tooltip` — the template uses
    ``{% if r.authority_tooltip %}`` to skip the title attribute
    when the value isn't in the table. Falls through to the raw
    string for unknown values so a custom-corpus authority like
    ``"slack-handbook"`` still surfaces as a chip even without a
    tooltip.
    """
    if not authority:
        return None
    return _AUTHORITY_TOOLTIPS.get(authority)


# Rail-filter field set (#273). Order doesn't matter for the count,
# but pinning the list here means a future field addition lands in
# both the helper AND its tests (the unit suite uses these names).
# Each value is one of:
#   * a string  — treated as active iff .strip() is truthy (matches
#     forms.split_csv which returns [] for whitespace-only input)
#   * a list    — treated as active iff non-empty
_RAIL_FIELDS: tuple[str, ...] = (
    "repo",
    "doc_type",
    "owner",
    "last_reviewed_after",
    "tags",
)


def rail_filters_active_count(filters_view: dict[str, Any]) -> int:
    """Count how many rail filter fields carry a real constraint (#273).

    A field counts iff its value would propagate as a non-None
    constraint to the retrieval engine. Mirrors :func:`forms.split_csv`
    and :func:`forms.filters_from_form` so the visual badge can't
    drift from what the engine actually sees.

    The template reads the return value twice:

    * ``{% if rail_filters_active_count(...) %}<details open>`` so a
      filter is never hidden behind a default-closed rail.
    * ``"· N active"`` suffix on the summary label so the count is
      visible while the rail is collapsed.

    Safe against missing keys / ``None`` values so a future template
    fixture or partial view dict doesn't KeyError.
    """
    count = 0
    for field in _RAIL_FIELDS:
        value = filters_view.get(field)
        if value is None:
            continue
        if isinstance(value, list):
            if value:
                count += 1
        elif isinstance(value, str):
            if value.strip():
                count += 1
        else:
            # Defensive: an unexpected type (int? date?) is treated as
            # truthy iff bool() agrees. Never silently drops a
            # constraint a future field type might carry.
            if value:
                count += 1
    return count


# Feedback-widget category table (#278). Single source of truth for
# the (signal, visible label, tooltip / aria explanation) triple. The
# template iterates this; the unit suite re-uses it to assert each
# rendered button carries the matching title + aria-label. The signal
# values are members of :data:`forms.FEEDBACK_TYPES` — the engine
# enum the /feedback POST handler validates against.
#
# Editorial voice for tooltips: second person, present tense, single
# sentence, capitalized, period. The labels stay terse because the
# widget renders as an inline italic phrase at the foot of the card;
# the tooltips carry the disambiguation the audit (#268) called out.
_FEEDBACK_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("useful", "yes", "This answered my question."),
    ("not_useful", "no", "This didn't answer my question."),
    ("stale", "stale", "The content is out of date."),
    ("wrong_source", "wrong source", "Not the right doc for this question."),
    (
        "missing_source",
        "missing source",
        "I expected a different doc to show up.",
    ),
    (
        "duplicate_or_conflicting",
        "duplicate",
        "Duplicate of another result, or conflicts with one.",
    ),
)


def feedback_categories() -> tuple[tuple[str, str, str], ...]:
    """Return the (signal, label, tooltip) table for the feedback widget.

    Stable order — the binary 'yes/no' leads, the four diagnostic
    categories follow. A future re-order that puts 'duplicate' first
    would bury the primary signal.

    Returned as a tuple of tuples so the template can iterate it via
    ``{% for signal, label, tooltip in feedback_categories() %}`` and
    the engine enum (``forms.FEEDBACK_TYPES``) stays the single
    source of truth for the SET of valid signals.
    """
    return _FEEDBACK_CATEGORIES


# #357 default: when the operator hasn't set KILN_AGENT_GUIDE_URL,
# we still want the colophon "Agents" link to appear so a developer
# landing on the kiln has a discoverable path to the agent surface.
# /docs#tag/agent is the FastAPI-generated Swagger UI scrolled to the
# agent operation group — always same-origin, always available
# wherever the kiln is reachable. Operators with a curated external
# guide override by setting KILN_AGENT_GUIDE_URL. Operators who want
# the link OFF set KILN_AGENT_GUIDE_URL=disabled (special sentinel).
_AGENT_GUIDE_URL_DEFAULT = "/docs#tag/agent"
_AGENT_GUIDE_URL_DISABLED = "disabled"


def agent_guide_url() -> str | None:
    """Return the configured agent-integration-guide URL, or the same-
    origin default (#314 / #357).

    Reads ``KILN_AGENT_GUIDE_URL`` (optional setting). Behavior:

    * Unset (default) → returns ``"/docs#tag/agent"``. The Swagger
      UI scrolled to the agent endpoints; always same-origin,
      always available (#357 changed this from None to default-on).
    * ``"disabled"`` sentinel → returns None. Operator off-switch for
      stock deploys that want zero colophon noise.
    * Same-origin absolute path (``"/..."``) → returned verbatim.
    * ``https://`` / ``http://`` → returned verbatim.
    * Anything else (``javascript:``, ``data:``, protocol-relative
      ``//``, etc.) → WARNING log + None (link silently dropped,
      NOT rendered as a broken/dangerous link).

    Security: the value goes into an ``href`` attribute, so we reject
    anything that isn't a safe URL scheme. Operator misconfiguration
    or an env-injection attack that sets the var to ``javascript:…``
    or ``data:…`` would otherwise execute attacker JS on every
    rendered page (Jinja autoescape doesn't sanitize URL schemes).

    Returned via the live ``get_settings()`` lookup (not a cached
    module-level constant) so tests + runtime overrides of the
    env var pick up immediately. The Jinja template registration
    in ``api/web.py`` wires this into the global namespace.
    """
    from cf_knowledge_kiln.config import get_settings

    raw = get_settings().agent_guide_url
    if raw is None:
        # #357: default-on. Same-origin Swagger UI is always available.
        return _AGENT_GUIDE_URL_DEFAULT
    candidate = raw.strip()
    if not candidate:
        return _AGENT_GUIDE_URL_DEFAULT
    if candidate == _AGENT_GUIDE_URL_DISABLED:
        # #357 explicit off-switch: operator chose to hide the link.
        return None
    # Same-origin absolute path is always safe.
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    # Otherwise require an explicit http(s) scheme. Anything else
    # (javascript:, data:, vbscript:, mailto:, custom-scheme:) is
    # refused so the template can't render a hostile href.
    if candidate.startswith(("https://", "http://")):
        return candidate
    logger.warning(
        "agent_guide_url: refusing non-http(s) scheme %r; check KILN_AGENT_GUIDE_URL",
        candidate[:32],
    )
    return None


def split_warnings(
    warnings: list[dict[str, Any]],
    result_document_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Bucket humanized warnings into (query-global, per-document) (#257).

    A warning with a ``source_id`` that matches a visible result's
    document_id is attached to that card; everything else (including
    warnings whose source_id matches a chunk that didn't make it
    into the visible top-K) stays at the top.

    Returns ``(global_warnings, per_document_warnings)`` where the
    per-document map is keyed by ``document_id`` (string form).
    """
    global_warnings: list[dict[str, Any]] = []
    per_doc: dict[str, list[dict[str, Any]]] = {}
    for w in warnings:
        sid = w.get("source_id")
        if sid is not None and sid in result_document_ids:
            per_doc.setdefault(sid, []).append(w)
        else:
            global_warnings.append(w)
    return global_warnings, per_doc


async def log_human_query(
    session: AsyncSession,
    *,
    query: str,
    filters: RetrievalFilters,
    chunk_ids: list[Any],
    request_id: str | None = None,
    requester: str | None = None,
) -> object | None:
    """Append a row to ``rag_queries`` and return its id (or None on failure).

    The id is rendered into the result page so the feedback widget
    can tie each rag_feedback row back to the query that produced
    the chunk. Non-fatal: telemetry failure logs + returns None,
    which the template renders without feedback widgets (rather
    than 500'ing the user's search).

    ``request_id`` (#260): the X-Request-ID correlation key from the
    middleware. Optional so a bare test harness that calls this
    function without installing the middleware still works.

    ``requester`` (#315): the OIDC username claim, threaded from the
    OIDC middleware via :func:`cf_knowledge_kiln.api.auth.username_for`.
    None outside of ``KILN_AUTH_MODE=oidc`` — the column is nullable.
    """
    try:
        async with session.begin_nested():
            row = await QueriesRepository(session).create(
                query=query,
                consumer_type="human",
                filters=filters.model_dump(exclude_none=True),
                retrieved_chunk_ids=chunk_ids,
                request_id=request_id,
                requester=requester,
            )
        return row.id
    except Exception:
        logger.exception("rag_queries telemetry write failed (non-fatal)")
        return None


__all__ = [
    "agent_guide_url",
    "authority_tooltip",
    "authority_vocabulary",
    "deprecation_label",
    "feedback_categories",
    "freshness_bucket",
    "freshness_label",
    "humanize_warning",
    "log_human_query",
    "rail_filters_active_count",
    "score_legend_tiers",
    "score_tier",
    "split_warnings",
    "status_tooltip",
    "warning_severity",
]
