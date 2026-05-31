"""Staleness signal for result-card freshness (#408 F17).

Extracted from :mod:`cf_knowledge_kiln.api.views` (which sits at the
400-line AGENTS soft cap) so the two helpers + the threshold table
live in one focused module. Pure functions — no I/O, no global
state — so the tests run in milliseconds.

For a cited-search product, age IS provenance. The 4-bucket
treatment grades visual weight so a user scanning at speed knows
whether the cited doc is "yesterday" or "three years ago" without
doing calendar math.

Bucket thresholds tuned for ops/runbook content:

* ``fresh``    — < 180 days  (well within typical doc-review cadence)
* ``recent``   — 180-365 d   (still trustworthy)
* ``aging``    — 365-730 d   (caution; muted-amber treatment)
* ``stale``    — > 730 days, OR future-dated (metadata error; oxblood)

Operators with shorter review cycles (security-runbook corpus where
any 90-day-old doc is suspect) can adjust by editing the constants
or — in a future config-driven pass — by exposing them via
``KILN_FRESHNESS_*`` env vars.

A future-dated review (clock skew, bad metadata) is bucketed as
``stale`` so the visible chip telegraphs the issue. The label
falls back to the raw ISO date in that case so the operator sees
the actual value and can investigate.
"""

from __future__ import annotations

from datetime import date

_FRESH_DAYS: int = 180
_RECENT_DAYS: int = 365
_AGING_DAYS: int = 730


def freshness_bucket(last_reviewed: date | None, *, today: date | None = None) -> str | None:
    """Bucket a ``last_reviewed`` date into a staleness class.

    Returns one of ``"fresh" | "recent" | "aging" | "stale"`` or
    ``None`` when the input is ``None`` (no review date on the doc
    → no staleness chrome; absence is its own kind of signal that
    the corpus-level "unreviewed" warning surfaces separately).

    Pure function. ``today`` argument is testable; production callers
    pass ``date.today()``. Future-dated ``last_reviewed`` (clock skew,
    bad metadata) buckets as ``"stale"`` — the visible chip
    telegraphs the issue and the operator sees the raw ISO label.
    """
    if last_reviewed is None:
        return None
    if today is None:
        today = date.today()
    delta_days = (today - last_reviewed).days
    if delta_days < 0:
        # Future-dated → metadata error. Bucket as stale so the
        # editorial styling calls attention; the label falls back
        # to the raw ISO so the operator sees the actual value.
        return "stale"
    if delta_days < _FRESH_DAYS:
        return "fresh"
    if delta_days < _RECENT_DAYS:
        return "recent"
    if delta_days < _AGING_DAYS:
        return "aging"
    return "stale"


def freshness_label(last_reviewed: date | None, *, today: date | None = None) -> str | None:
    """Return ``"Reviewed N {unit} ago"`` or ``None`` for absent dates.

    Unit ladder: days (< 60), months (< 24), years. Singular/plural
    handled. ``"Reviewed today"`` for delta=0; ``"Reviewed yesterday"``
    for delta=1. Future-dated reviews fall back to the raw ISO date
    so the user sees the bad metadata directly.
    """
    if last_reviewed is None:
        return None
    if today is None:
        today = date.today()
    delta_days = (today - last_reviewed).days
    if delta_days < 0:
        # Future-dated. Don't render an awkward "-3 days ago" — fall
        # back to the bare ISO date so the user sees the raw value
        # and understands something is off.
        return f"Reviewed {last_reviewed.isoformat()}"
    if delta_days == 0:
        return "Reviewed today"
    if delta_days == 1:
        return "Reviewed yesterday"
    if delta_days < 60:
        return f"Reviewed {delta_days} days ago"
    months = delta_days // 30
    if months < 24:
        unit = "month" if months == 1 else "months"
        return f"Reviewed {months} {unit} ago"
    years = delta_days // 365
    unit = "year" if years == 1 else "years"
    return f"Reviewed {years} {unit} ago"


__all__ = ["freshness_bucket", "freshness_label"]
