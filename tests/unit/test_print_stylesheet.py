"""Pins the #341 fix: a print stylesheet ships in the bundle.

Audit Finding #30. Without these rules the printed/PDF-saved page
loses URLs on links, forces dark background onto white paper,
hides deprecation stripes, and bleeds the preview panel into the
results list.

Tests assert two contracts:
* The standalone partial exists in the kiln/ partials directory.
* The Makefile includes the partial in the bundled kiln.css.
* The bundled kiln.css carries an @media print block.
* The print block hides chrome (htmx-bar, skip-link, preview, etc).
* The print block restores URLs after links via ::after content.
* The print block forces light theme regardless of OS pref.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PARTIAL = _REPO / "src/cf_knowledge_kiln/api/static/kiln/_print.css"
_BUNDLE = _REPO / "src/cf_knowledge_kiln/api/static/kiln.css"
_MAKEFILE = _REPO / "Makefile"


class TestPrintStylesheetPartial:
    def test_partial_exists(self) -> None:
        assert _PARTIAL.exists(), (
            "The print stylesheet partial must live at "
            "src/cf_knowledge_kiln/api/static/kiln/_print.css "
            "so make build-css can fold it into the bundle."
        )

    def test_partial_wraps_rules_in_media_print(self) -> None:
        text = _PARTIAL.read_text()
        # Every rule in this partial should sit inside @media print —
        # otherwise screen styles pick them up too, defeating the
        # purpose. Pin the outer guard. The actual declaration syntax
        # is `@media print {`; comments above may say "@media print"
        # too, so anchor on the brace.
        assert "@media print {" in text
        # Exactly one @media print rule block.
        assert text.count("@media print {") == 1


class TestPrintStylesheetBundled:
    def test_makefile_includes_print_partial(self) -> None:
        text = _MAKEFILE.read_text()
        assert "_print.css" in text, (
            "Makefile's build-css target must cat _print.css into "
            "kiln.css; otherwise the partial doesn't reach the "
            "browser when an operator hits Print."
        )

    def test_bundle_carries_media_print_block(self) -> None:
        """Regression guard: if make build-css drifts (forgets to
        regenerate after a partial change), the bundle won't carry
        the print rules. make verify-css catches it in CI; this
        test catches it locally before the push."""
        text = _BUNDLE.read_text()
        assert "@media print" in text


class TestPrintBlockContract:
    """Behaviour pins on the @media print block itself. We grep
    the partial (not the bundle) so a change to the inclusion
    order doesn't accidentally pass this test."""

    def _body(self) -> str:
        return _PARTIAL.read_text()

    def test_forces_light_theme(self) -> None:
        body = self._body()
        # The dark-mode preference must NOT bleed onto paper.
        assert "color-scheme: light" in body
        assert "background: #fff" in body
        assert "color: #111" in body

    def test_hides_interactive_chrome(self) -> None:
        body = self._body()
        # A handful of must-be-hidden selectors; if any of these
        # ever survive into print, the page is a mess.
        for selector in (
            ".htmx-bar",
            ".filter-rail",
            ".filters",
            ".preview-panel",
            "#cheatsheet",
            ".skip-link",
            "#search-status",
            "#feedback-status",
            "#toast",
        ):
            assert selector in body, (
                f"@media print MUST hide {selector!r} — it has no "
                f"value on paper and clutters the output."
            )

    def test_restores_urls_after_links(self) -> None:
        """Without href-after rules the printed page loses every
        link's destination. The audit found this was the dominant
        loss-of-context complaint."""
        body = self._body()
        assert 'content: " [" attr(href) "]"' in body
        # Result-title button has its target as hx-get (not href),
        # so pin that too.
        assert 'content: " [" attr(hx-get) "]"' in body

    def test_avoids_card_split_across_pages(self) -> None:
        """page-break-inside: avoid keeps each result card on one
        page — a card split across pages is unreadable."""
        body = self._body()
        assert "page-break-inside: avoid" in body

    def test_keeps_deprecation_signal(self) -> None:
        """Deprecation can't rely on the stripe (color-dependent) on
        paper. The print block should swap to a typographic signal:
        strikethrough on the title + bracketed stamp text."""
        body = self._body()
        assert "text-decoration: line-through" in body
        # The stamp annotation is wrapped in brackets via ::before/::after.
        assert 'content: "["' in body
        assert 'content: "]"' in body
