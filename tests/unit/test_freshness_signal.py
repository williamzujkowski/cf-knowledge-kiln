"""Unit tests for the #408 F17 staleness-signal helpers.

The view helpers are pure functions — bucket boundaries + relative-
time labels. Pin both so a future threshold change is forced through
test updates (cited-search trust signal — drifting silently is bad).
"""

from __future__ import annotations

from datetime import date, timedelta

from cf_knowledge_kiln.api.views import freshness_bucket, freshness_label

_TODAY = date(2026, 5, 31)  # Fixed reference so tests don't drift over wall-clock days.


class TestFreshnessBucket:
    """Bucket boundaries: fresh < 180 < recent < 365 < aging < 730 < stale."""

    def test_none_returns_none(self) -> None:
        assert freshness_bucket(None, today=_TODAY) is None

    def test_today_is_fresh(self) -> None:
        assert freshness_bucket(_TODAY, today=_TODAY) == "fresh"

    def test_30_days_ago_is_fresh(self) -> None:
        assert freshness_bucket(_TODAY - timedelta(days=30), today=_TODAY) == "fresh"

    def test_179_days_ago_is_fresh(self) -> None:
        """Right under the boundary — fresh."""
        assert freshness_bucket(_TODAY - timedelta(days=179), today=_TODAY) == "fresh"

    def test_180_days_ago_is_recent(self) -> None:
        """At the boundary — graduates to recent. Pin the inclusive
        side so a refactor that flips < vs <= is caught."""
        assert freshness_bucket(_TODAY - timedelta(days=180), today=_TODAY) == "recent"

    def test_364_days_ago_is_recent(self) -> None:
        assert freshness_bucket(_TODAY - timedelta(days=364), today=_TODAY) == "recent"

    def test_365_days_ago_is_aging(self) -> None:
        assert freshness_bucket(_TODAY - timedelta(days=365), today=_TODAY) == "aging"

    def test_729_days_ago_is_aging(self) -> None:
        assert freshness_bucket(_TODAY - timedelta(days=729), today=_TODAY) == "aging"

    def test_730_days_ago_is_stale(self) -> None:
        assert freshness_bucket(_TODAY - timedelta(days=730), today=_TODAY) == "stale"

    def test_3_years_ago_is_stale(self) -> None:
        assert freshness_bucket(_TODAY - timedelta(days=365 * 3), today=_TODAY) == "stale"

    def test_future_date_treated_as_fresh(self) -> None:
        """Clock skew / bad metadata: future-dated review shouldn't
        crash or render '-N days ago'. Fall back to fresh."""
        future = _TODAY + timedelta(days=30)
        assert freshness_bucket(future, today=_TODAY) == "fresh"

    def test_default_today_uses_real_date(self) -> None:
        """Without the ``today=`` arg, the helper consults date.today().
        Pin that the function doesn't require the kwarg."""
        # date 1 year before today — should land in 'aging' regardless
        # of when this test runs (1 yr = 365 d ≥ 365 d boundary).
        one_year_ago = date.today() - timedelta(days=400)
        bucket = freshness_bucket(one_year_ago)
        assert bucket in ("aging", "stale")  # 400 days lands in aging; 730+ → stale


class TestFreshnessLabel:
    """Relative-time string. Singular/plural + unit ladder."""

    def test_none_returns_none(self) -> None:
        assert freshness_label(None, today=_TODAY) is None

    def test_today(self) -> None:
        assert freshness_label(_TODAY, today=_TODAY) == "Reviewed today"

    def test_yesterday(self) -> None:
        assert freshness_label(_TODAY - timedelta(days=1), today=_TODAY) == "Reviewed yesterday"

    def test_days_ago_plural(self) -> None:
        assert freshness_label(_TODAY - timedelta(days=14), today=_TODAY) == "Reviewed 14 days ago"

    def test_two_days_ago(self) -> None:
        # Edge case: should NOT collide with "yesterday".
        assert freshness_label(_TODAY - timedelta(days=2), today=_TODAY) == "Reviewed 2 days ago"

    def test_under_60_days_renders_as_days(self) -> None:
        """Days bucket runs < 60. Pin so the threshold doesn't drift."""
        assert freshness_label(_TODAY - timedelta(days=35), today=_TODAY) == "Reviewed 35 days ago"
        assert freshness_label(_TODAY - timedelta(days=59), today=_TODAY) == "Reviewed 59 days ago"

    def test_two_months_singular_vs_plural(self) -> None:
        # 60 days → months bucket, 60/30 = 2 months → plural.
        assert freshness_label(_TODAY - timedelta(days=60), today=_TODAY) == "Reviewed 2 months ago"
        # 35 days → still 'days' bucket (< 60). Above already covers.
        # ≥ 60 d but only 1 month would require 30-59 d, which the
        # 'days' bucket owns. So 'Reviewed 1 month ago' is unreachable
        # under the current threshold ladder. Document the choice via
        # a no-rendering test for the 30-59 range:
        out = freshness_label(_TODAY - timedelta(days=30), today=_TODAY)
        assert "1 month" not in out  # never rendered; days bucket wins

    def test_six_months(self) -> None:
        assert (
            freshness_label(_TODAY - timedelta(days=180), today=_TODAY) == "Reviewed 6 months ago"
        )

    def test_one_year_singular(self) -> None:
        # 730 days = ~ 2 years (24 months). Below 24 months stays in
        # months. ≥ 24 months graduates to years.
        # 365 days = 12 months. Still in months bucket per the
        # ladder. 730+ days = years bucket.
        out = freshness_label(_TODAY - timedelta(days=365 + 365), today=_TODAY)
        assert "year" in out

    def test_multi_year(self) -> None:
        out = freshness_label(_TODAY - timedelta(days=365 * 3), today=_TODAY)
        assert out == "Reviewed 3 years ago"

    def test_future_date_falls_back_to_iso(self) -> None:
        """Future dates render the raw ISO string so the user can
        spot the metadata issue. Don't render '-N days ago'."""
        future = _TODAY + timedelta(days=30)
        out = freshness_label(future, today=_TODAY)
        assert out is not None
        assert future.isoformat() in out


class TestResultCardViewSurfacesFreshness:
    """Integration with :func:`result_card_view` — the dict that the
    template consumes must carry both bucket + label."""

    def test_view_includes_freshness_bucket(self) -> None:
        from types import SimpleNamespace

        from cf_knowledge_kiln.api.result_cards import result_card_view

        chunk = SimpleNamespace(
            chunk_id="c1",
            document_id="d1",
            heading_path=["x"],
            status="active",
            last_reviewed=date(2024, 1, 1),  # well past the aging threshold
            score=0.5,
            chunk_index=0,
            chunk_count=1,
            authority=None,
        )
        view = result_card_view(chunk, ref=None, content="hello", query="")
        # Bucket present + plausible (aging/stale depending on
        # today's date — both indicate the chip surfaces).
        assert "freshness_bucket" in view
        assert view["freshness_bucket"] in ("aging", "stale")
        # Label too.
        assert "freshness_label" in view
        assert view["freshness_label"] is not None
        assert "Reviewed" in view["freshness_label"]

    def test_view_handles_none_last_reviewed(self) -> None:
        """Chunks without a review date → both fields are None so the
        template's existing ``{% if r.last_reviewed %}`` guard
        short-circuits cleanly."""
        from types import SimpleNamespace

        from cf_knowledge_kiln.api.result_cards import result_card_view

        chunk = SimpleNamespace(
            chunk_id="c1",
            document_id="d1",
            heading_path=["x"],
            status="active",
            last_reviewed=None,
            score=0.5,
            chunk_index=0,
            chunk_count=1,
            authority=None,
        )
        view = result_card_view(chunk, ref=None, content="hello", query="")
        assert view["freshness_bucket"] is None
        assert view["freshness_label"] is None


class TestTemplatePinFreshnessClass:
    """Source-grep that the template applies the bucket class so a
    future refactor that drops the class breaks this test rather
    than silently regressing the visual treatment."""

    def test_template_emits_freshness_bucket_class(self) -> None:
        from pathlib import Path as _Path

        src = (
            _Path(__file__).resolve().parents[2]
            / "src"
            / "cf_knowledge_kiln"
            / "api"
            / "templates"
            / "_results.html"
        ).read_text(encoding="utf-8")
        assert "freshness-{{ r.freshness_bucket }}" in src, (
            "Template must apply the freshness-X bucket class to the "
            "<time> element so the CSS can grade staleness."
        )

    def test_template_uses_freshness_label_when_present(self) -> None:
        from pathlib import Path as _Path

        src = (
            _Path(__file__).resolve().parents[2]
            / "src"
            / "cf_knowledge_kiln"
            / "api"
            / "templates"
            / "_results.html"
        ).read_text(encoding="utf-8")
        assert "r.freshness_label" in src, (
            "Template must prefer the relative-time label so users "
            "don't do calendar math at scan speed."
        )

    def test_freshness_css_partial_exists(self) -> None:
        from pathlib import Path as _Path

        partial = (
            _Path(__file__).resolve().parents[2]
            / "src"
            / "cf_knowledge_kiln"
            / "api"
            / "static"
            / "kiln"
            / "_freshness.css"
        )
        assert partial.exists(), "_freshness.css partial must ship with the bucket styles."
        css = partial.read_text(encoding="utf-8")
        # Pin each bucket class so the partial can't drift from the
        # bucket vocabulary in views.freshness_bucket.
        for cls in (".freshness-recent", ".freshness-aging", ".freshness-stale"):
            assert cls in css, f"_freshness.css missing {cls} rule"


class TestForcedColorsCoverage:
    """A11y: the staleness signal must survive Windows High Contrast
    (forced-colors). Currently the editorial palette (oxblood/gold)
    gets stripped under forced-colors; the partial must declare an
    explicit forced-colors block."""

    def test_partial_has_forced_colors_block(self) -> None:
        from pathlib import Path as _Path

        css = (
            _Path(__file__).resolve().parents[2]
            / "src"
            / "cf_knowledge_kiln"
            / "api"
            / "static"
            / "kiln"
            / "_freshness.css"
        ).read_text(encoding="utf-8")
        assert "@media (forced-colors: active)" in css
        # And the block must address at minimum the stale bucket
        # (the strongest signal — must survive WHC).
        forced_idx = css.find("@media (forced-colors: active)")
        forced_block = css[forced_idx : forced_idx + 2000]
        assert ".freshness-stale" in forced_block
