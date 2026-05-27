"""Pins the #345 fix: preview close restores focus to the card.

A keyboard user activates a result-title button → preview drawer
slides in. They hit Esc (or backdrop / close-button). Without
this fix, focus lands on <body> and the user has to Tab from the
top of the page back to where they were.

The fix wires three pieces:
* `_results.html` puts `data-chunk-id` on the result-title button
  (the click target, what the user actually focused).
* `kiln-app.js::_openPreview(opener)` reads that chunk-id and
  stores it on the preview panel as `data-opener-key`.
* `kiln-app.js::_closePreview()` queries back to the matching
  result-title button via that key and `.focus()`es it inside a
  requestAnimationFrame.

JS-source-grep tests (per repo precedent for kiln-app.js) pin the
key contract strings + the focus call.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates"
_KILN_APP_JS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln-app.js"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


class TestResultsTemplateMarksFocusReturnTarget:
    """The result-title button must carry data-chunk-id so the JS
    can query it back at close-time. Without this attribute the
    focus-return mechanism silently degrades — closing the drawer
    lands on <body> as before."""

    def test_result_button_has_data_chunk_id(self) -> None:
        # Source-grep the template directly. Rendering it requires
        # a full result-card fixture (status_tooltip, score_tier,
        # tooltips, related sources, the lot) that's not worth
        # reconstructing — the file:line pin proves the attribute
        # is on the button, which is the contract the JS depends on.
        results_html = (_TEMPLATES / "_results.html").read_text()
        # Find the result-title-button opening tag.
        idx = results_html.index('class="result-title-button"')
        # Window covers the multi-line opening tag including
        # data-chunk-id; the inline comment between data-action and
        # data-chunk-id pushes the latter past 600 chars.
        button_block = results_html[idx : idx + 1200]
        assert 'data-chunk-id="{{ r.chunk_id }}"' in button_block, (
            "The result-title-button MUST carry data-chunk-id so "
            "kiln-app.js can restore focus to it after the preview "
            "drawer closes (#345)."
        )


class TestKilnAppFocusReturnContract:
    """Pin the contract strings the JS uses for focus-return so a
    refactor that drops one piece is caught immediately."""

    def _source(self) -> str:
        return _KILN_APP_JS.read_text()

    def test_openpreview_accepts_opener_arg(self) -> None:
        source = self._source()
        # The new signature is `_openPreview = (opener) => ...` so
        # the caller can pass the activated control.
        assert "_openPreview = (opener)" in source

    def test_openpreview_records_opener_key_on_panel(self) -> None:
        """The opener's chunk-id is stored on the panel for close-time
        retrieval. Storing the element ref directly would go stale
        across HTMX swaps; the chunk-id survives because cards
        rendered for the same query keep the same id."""
        source = self._source()
        assert "p.dataset.openerKey = opener.dataset.chunkId" in source

    def test_closepreview_queries_button_by_chunk_id(self) -> None:
        """At close time, re-query the document for the button bearing
        the stored chunk-id (not the cached element ref)."""
        source = self._source()
        assert '.result-title-button[data-chunk-id="' in source and "p.dataset.openerKey" in source

    def test_closepreview_calls_focus_via_raf(self) -> None:
        """Focus must be deferred to the next animation frame so the
        browser commits the data-open removal first; otherwise the
        focus race causes the panel to briefly stay focusable."""
        source = self._source()
        # Within _closePreview's body, expect requestAnimationFrame
        # wrapping the .focus() call. Widen the window to 1500 chars
        # so we capture the trailing `requestAnimationFrame(() =>
        # opener.focus())` line.
        start = source.index("_closePreview = ()")
        body = source[start : start + 1500]
        assert "requestAnimationFrame" in body
        assert "opener.focus()" in body

    def test_dispatcher_passes_actor_to_openpreview(self) -> None:
        """The click delegator must pass the activated [data-action]
        element to _openPreview, not call it bare. Without the arg,
        the chunk-id is never recorded and focus return silently
        fails."""
        source = self._source()
        # The dispatch site calls _openPreview(actor); the alternative
        # _openPreview() with no arg would re-create the original bug.
        assert "_openPreview(actor)" in source
        assert "_openPreview()" not in source  # regression guard

    def test_closepreview_clears_opener_key_after_focus(self) -> None:
        """Belt-and-braces: don't leave the opener-key dangling on the
        panel between close + next open. A stale key could cause the
        next open to record an out-of-date opener if the JS path is
        ever changed."""
        source = self._source()
        start = source.index("_closePreview = ()")
        body = source[start : start + 900]
        assert "delete p.dataset.openerKey" in body
