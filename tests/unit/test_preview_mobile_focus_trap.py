"""Pins the #346 fix: mobile preview drawer is a true focus trap.

When the preview opens on a ``(max-width: 959px)`` viewport it
visually obscures the page underneath. WCAG 2.4.3 (Focus Order)
+ the ARIA Authoring Practices "Modal Dialog" pattern both call
for the drawer to:

* carry ``role="dialog"`` + ``aria-modal="true"`` while open
* loop ``Tab`` + ``Shift+Tab`` focus inside the drawer
* on close, drop the role + aria-modal so the desktop sticky
  rail (``min-width: 960px``) doesn't get falsely announced as
  a modal

The fix lives in :file:`kiln-app.js` — JS-toggled rather than
template-static because ``data-open`` is set on the panel in both
the mobile-drawer and desktop-rail cases, so the modal semantics
must be gated on viewport, not on ``data-open`` alone.

Tests are source-text grep assertions (the repo precedent — see
:mod:`tests.unit.test_preview_focus_return`); no jsdom runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_KILN_APP_JS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln-app.js"
_PREVIEW_CSS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln" / "_preview.css"
_SEARCH_HTML = _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates" / "search.html"


@pytest.fixture(scope="module")
def js_source() -> str:
    return _KILN_APP_JS.read_text(encoding="utf-8")


class TestMobileMediaQueryPinned:
    """The mobile breakpoint must match :file:`_preview.css` (the
    drawer slide-in CSS uses ``(max-width: 959px)``). If the
    breakpoint drifts, the focus-trap fires on a viewport that
    isn't presenting as a modal (or fails to fire when it is)."""

    def test_focus_trap_uses_959px_breakpoint(self, js_source: str) -> None:
        # The existing focus-grab on open already uses this query
        # (kiln-app.js:102). The new trap helpers must reuse it
        # verbatim — searching for both ensures the new helpers
        # weren't introduced under a different (drifting) breakpoint.
        assert js_source.count('matchMedia("(max-width: 959px)")') >= 2, (
            "Mobile drawer focus trap must reuse the same "
            "(max-width: 959px) breakpoint as the existing focus-grab "
            "on open, so the trap fires iff the drawer is presenting "
            "as a modal."
        )

    def test_preview_css_still_uses_959px_breakpoint(self) -> None:
        """If someone changes the CSS breakpoint without updating
        the JS, the focus trap will fire on the wrong viewport.
        Pin both ends of the contract."""
        css = _PREVIEW_CSS.read_text(encoding="utf-8")
        assert "max-width: 959px" in css


class TestDialogRoleAppliedOnMobileOpen:
    """The drawer gets ``role="dialog"`` + ``aria-modal="true"``
    only when opened on a mobile viewport. The desktop sticky rail
    must NOT carry these — it isn't presenting as a modal and
    announcing it as one would mislead AT users."""

    def test_open_sets_role_dialog_on_panel(self, js_source: str) -> None:
        # Look for the assignment inside the _openPreview body.
        start = js_source.index("_openPreview = (opener)")
        body = js_source[start : start + 1200]
        assert 'setAttribute("role", "dialog")' in body, (
            "_openPreview must set role=dialog on the panel when the mobile drawer is presenting."
        )

    def test_open_sets_aria_modal_true_on_panel(self, js_source: str) -> None:
        start = js_source.index("_openPreview = (opener)")
        body = js_source[start : start + 1200]
        assert 'setAttribute("aria-modal", "true")' in body

    def test_role_assignment_is_mobile_gated(self, js_source: str) -> None:
        """The role + aria-modal assignment lives behind the same
        matchMedia gate as the existing focus-grab. Without the
        gate, desktop AT users hear 'modal' on the sticky rail —
        which the spec explicitly forbids."""
        start = js_source.index("_openPreview = (opener)")
        body = js_source[start : start + 1200]
        assert 'matchMedia("(max-width: 959px)")' in body, (
            "role=dialog + aria-modal must be applied behind a "
            "matchMedia gate so the desktop rail doesn't get them."
        )


class TestDialogRoleClearedOnClose:
    """On close the role + aria-modal must be removed so the
    next open on a different viewport (orientation flip, dev-tools
    resize) doesn't inherit stale modal semantics."""

    def test_close_removes_role(self, js_source: str) -> None:
        start = js_source.index("_closePreview = ()")
        body = js_source[start : start + 1500]
        assert 'removeAttribute("role")' in body

    def test_close_removes_aria_modal(self, js_source: str) -> None:
        start = js_source.index("_closePreview = ()")
        body = js_source[start : start + 1500]
        assert 'removeAttribute("aria-modal")' in body


class TestTabKeyTrapped:
    """Tab + Shift+Tab loop focus within the drawer when it's
    open on mobile. With the trap off, Tab walks out of the drawer
    into the obscured page (the original bug)."""

    def test_keydown_listener_handles_tab(self, js_source: str) -> None:
        # The trap handler is a keydown listener that compares e.key
        # against "Tab". Be permissive on the comparison form (===
        # for the cheatsheet pattern, !== for an early-return guard)
        # — both express the same semantic.
        forms = (
            'e.key === "Tab"',
            "e.key === 'Tab'",
            'e.key !== "Tab"',
            "e.key !== 'Tab'",
        )
        assert any(f in js_source for f in forms)

    @staticmethod
    def _tab_handler_window(source: str) -> str:
        """Locate the Tab handler body and return a generous window
        starting at the e.key check (covers both === and !== forms)."""
        for needle in (
            'e.key === "Tab"',
            "e.key === 'Tab'",
            'e.key !== "Tab"',
            "e.key !== 'Tab'",
        ):
            idx = source.find(needle)
            if idx != -1:
                return source[idx : idx + 2000]
        raise AssertionError("Tab handler not found")

    def test_trap_calls_preventdefault_on_tab(self, js_source: str) -> None:
        """The trap must preventDefault on the trapped Tab events;
        otherwise the browser's native focus advance fires too and
        the focused element ends up wrong (or outside the panel)."""
        window = self._tab_handler_window(js_source)
        assert "preventDefault()" in window


class TestFocusLoopsFirstAndLast:
    """The trap finds the focusable elements inside the panel and
    loops between the first and last. With only one focusable
    (degenerate case), both directions land on the same element —
    same shape as the cheatsheet trap (kiln-keys.js:188-198)."""

    def test_focusable_selector_includes_buttons_links_summaries(self, js_source: str) -> None:
        """The selector must cover what's actually focusable inside
        the panel: the close ``<button>``, the canonical-source
        ``<a>``, and the prev/next ``<summary>`` rows from
        ``_preview.html``. Missing any of these silently shrinks
        the trap target set and the user can still escape."""
        body = TestTabKeyTrapped._tab_handler_window(js_source)
        # querySelectorAll for the focusables. Keep the assertion
        # loose on exact selector tokens — just make sure each of
        # the three focusable shapes is named.
        assert "querySelectorAll" in body
        for token in ("button", "[href]", "summary"):
            assert token in body, (
                f"focusable selector inside the Tab trap must include "
                f"{token!r}; without it the trap misses a focusable "
                f"element and the user escapes the drawer."
            )

    def test_shift_tab_loops_backward(self, js_source: str) -> None:
        body = TestTabKeyTrapped._tab_handler_window(js_source)
        # The trap checks e.shiftKey to flip the loop direction.
        assert "e.shiftKey" in body

    def test_trap_loops_first_to_last(self, js_source: str) -> None:
        """Variable names ``first``/``last`` are part of the
        contract — easier to audit than positional indexing."""
        body = TestTabKeyTrapped._tab_handler_window(js_source)
        assert "first" in body and "last" in body


class TestPanelItselfTraps:
    """Regression guard: the focus-grab on open lands focus on the
    panel element itself (kiln-app.js:105). From that state, both
    Tab AND Shift+Tab must loop inside the panel — without the
    panel-itself carve-out on the forward-Tab branch, the user
    presses Tab and native focus advance walks them into the
    obscured page beneath the drawer."""

    def test_shift_tab_from_panel_loops_to_last(self, js_source: str) -> None:
        body = TestTabKeyTrapped._tab_handler_window(js_source)
        # Shift+Tab branch must accept active===p (the panel itself).
        # Pattern is ``e.shiftKey && (active === first || active === p)``.
        assert "active === p" in body, (
            "Shift+Tab handler must accept the panel itself as the "
            "from-state so it loops to last instead of letting native "
            "focus walk out of the drawer."
        )

    def test_forward_tab_from_panel_loops_to_first(self, js_source: str) -> None:
        """The forward-Tab branch must be symmetric with Shift+Tab.
        Without ``(active === last || active === p)`` here, a user
        whose focus is on the panel (the open-state) escapes the
        drawer on the very first Tab."""
        body = TestTabKeyTrapped._tab_handler_window(js_source)
        # Find the forward-Tab branch (the ``else if (!e.shiftKey ...``
        # block) and pin the panel-itself carve-out is there too.
        idx = body.find("!e.shiftKey")
        assert idx != -1, "forward-Tab branch missing"
        forward = body[idx : idx + 400]
        assert "active === p" in forward, (
            "Forward-Tab branch must accept the panel itself as the "
            "from-state (mirror of the Shift+Tab branch). Otherwise "
            "the very first Tab after open escapes the drawer."
        )


class TestViewportChangeStripsStaleDialog:
    """Regression guard: orientation flip / dev-tools resize from
    a mobile viewport (where role=dialog was applied) to a desktop
    viewport (where it would mislead AT) must strip role + aria-modal
    even without an intervening close. matchMedia ``change`` event
    is the standard hook."""

    def test_mq_change_listener_registered(self, js_source: str) -> None:
        # The standard contemporary API.
        assert "matchMedia" in js_source
        # The listener uses ``addEventListener("change", ...)`` —
        # pin the literal so a switch to ``onchange = ...`` (which
        # would clobber any other listeners) is caught.
        assert '"change"' in js_source

    def test_mq_change_handler_removes_role(self, js_source: str) -> None:
        """The change handler body must call removeAttribute('role')
        + removeAttribute('aria-modal') when the viewport crosses
        out of mobile range while the drawer is open."""
        # The handler is named _onViewportChange in the source.
        idx = js_source.find("_onViewportChange")
        assert idx != -1, "viewport-change handler missing"
        # Find the function body (the const declaration follows).
        body_start = js_source.find("=", idx)
        body = js_source[body_start : body_start + 600]
        assert 'removeAttribute("role")' in body
        assert 'removeAttribute("aria-modal")' in body

    def test_mq_change_handler_guards_on_data_open(self, js_source: str) -> None:
        """If the drawer is closed we don't need to strip anything —
        the close handler already did. The change handler must check
        ``data-open`` before touching anything, otherwise it would
        thrash the panel attributes on every viewport change."""
        idx = js_source.find("_onViewportChange")
        assert idx != -1
        body_start = js_source.find("=", idx)
        body = js_source[body_start : body_start + 600]
        assert "data-open" in body


class TestNoTrapOnDesktop:
    """Per the spec: desktop sticky rail is NOT a focus trap —
    it's a panel, not a modal. The Tab handler must early-return
    when the matchMedia check fails."""

    def test_tab_handler_early_returns_on_desktop(self, js_source: str) -> None:
        """The handler body must consult matchMedia and bail out
        when the viewport isn't the mobile drawer. We pin the
        idiomatic pattern ``.matches`` so a refactor that
        accidentally inverts the check (or drops it) is caught."""
        body = TestTabKeyTrapped._tab_handler_window(js_source)
        assert "matches" in body, (
            "Tab handler must guard on matchMedia(...).matches so the "
            "desktop sticky rail doesn't trap focus."
        )

    def test_tab_handler_requires_data_open(self, js_source: str) -> None:
        """The trap also early-returns when the drawer isn't open
        (a closed drawer has no focusables and the trap would
        either no-op or accidentally pull focus to a hidden
        button)."""
        body = TestTabKeyTrapped._tab_handler_window(js_source)
        assert "data-open" in body


class TestSearchTemplateContract:
    """The panel container in :file:`search.html` keeps its
    ``id="preview"`` + ``tabindex="-1"`` (the JS depends on
    both). This is the same contract the existing focus-grab
    relies on; pin it again so a template rename breaks both
    tests together."""

    def test_panel_has_id_preview(self) -> None:
        assert 'id="preview"' in _SEARCH_HTML.read_text(encoding="utf-8")

    def test_panel_is_programmatically_focusable(self) -> None:
        html = _SEARCH_HTML.read_text(encoding="utf-8")
        idx = html.index('id="preview"')
        block = html[idx : idx + 400]
        assert 'tabindex="-1"' in block, (
            "#preview must remain tabindex=-1 (programmatically "
            "focusable but not Tab-reachable) so the focus-grab on "
            "open lands on the panel and the trap is the first thing "
            "to handle Tab."
        )
