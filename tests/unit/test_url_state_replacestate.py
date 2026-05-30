"""Issue #371: ``history.replaceState`` + ``popstate`` wiring on the
HTMX search form so the URL reflects the current filter state.

Two pieces:

* After every search submit (HTMX ``htmx:afterRequest`` for the
  search form), serialise the form to ``URLSearchParams`` and call
  ``history.replaceState`` so the URL updates without reloading.
* On browser back/forward (``popstate``), re-populate the form
  fields from the URL and re-fire the HTMX search so the page
  matches the URL.

Tests are source-text grep assertions (repo precedent — no jsdom;
see :mod:`tests.unit.test_preview_focus_return`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_KILN_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cf_knowledge_kiln"
    / "api"
    / "static"
    / "kiln-app.js"
)


@pytest.fixture(scope="module")
def js_source() -> str:
    return _KILN_APP_JS.read_text(encoding="utf-8")


class TestReplaceStateOnSubmit:
    """After every successful HTMX search the URL must update via
    ``history.replaceState`` (not pushState — we don't want every
    keystroke producing a back-button entry; the user's last URL
    pre-search is the back target)."""

    def test_uses_replacestate_not_pushstate(self, js_source: str) -> None:
        assert "history.replaceState" in js_source, (
            "URL updates must use replaceState (not pushState) so "
            "300ms-debounced keystrokes don't pollute browser history."
        )
        assert "history.pushState" not in js_source, (
            "pushState would put every debounced keystroke into the "
            "back-button stack — confirm replaceState is the only path."
        )

    def test_listens_on_htmx_afterrequest(self, js_source: str) -> None:
        """The URL update must fire on ``htmx:afterRequest`` (the
        response landed successfully) rather than
        ``htmx:configRequest`` (would update on every keystroke even
        if the request fails) or ``htmx:beforeRequest`` (would update
        before knowing if the response succeeds)."""
        assert "htmx:afterRequest" in js_source

    def test_url_built_from_search_form(self, js_source: str) -> None:
        """The URL must be built from the search form's data —
        ``new FormData(form)`` + ``URLSearchParams``. Pin both so a
        refactor that drops one is caught."""
        # The handler reads from form.search-form (the only POST source
        # we care about — the feedback form has its own widget and we
        # never want feedback POSTs altering the URL).
        assert "form.search-form" in js_source
        assert "URLSearchParams" in js_source


class TestPopstateRePopulates:
    """When the user hits back / forward, ``popstate`` fires. The
    handler must re-populate the form fields from the new URL and
    re-fire the HTMX search so the page matches what the URL says."""

    def test_registers_popstate_listener(self, js_source: str) -> None:
        assert '"popstate"' in js_source or "'popstate'" in js_source

    def test_popstate_handler_reads_location_search(self, js_source: str) -> None:
        """The handler must read the new URL's query string —
        ``window.location.search`` is the canonical source."""
        # Locate the popstate handler.
        idx = js_source.find("popstate")
        assert idx != -1
        body = js_source[idx : idx + 2000]
        assert "location.search" in body

    def test_popstate_handler_retriggers_htmx_submit(self, js_source: str) -> None:
        """After re-populating, the handler must re-fire the search.
        ``htmx.trigger(form, "submit")`` is the canonical way.

        Anchor on the ``addEventListener("popstate"...)`` registration
        (not the first ``popstate`` mention, which is inside a comment)
        so the search window covers the actual handler body."""
        idx = js_source.find('addEventListener("popstate"')
        if idx == -1:
            idx = js_source.find("addEventListener('popstate'")
        assert idx != -1, "popstate listener must be registered"
        body = js_source[idx : idx + 2500]
        # Either form of trigger is acceptable.
        assert ("htmx.trigger" in body) or ("dispatchEvent" in body)


class TestFormFieldPopulation:
    """The popstate handler must walk the URL params and assign each
    one to the matching form field. Status checkboxes (multi-value)
    + text/date inputs (single) are different shapes — pin both."""

    def test_populates_query_input(self, js_source: str) -> None:
        idx = js_source.find("popstate")
        assert idx != -1
        body = js_source[idx : idx + 2000]
        # Either named ``query`` (matches form name) or ``q`` (URL param).
        assert "query" in body or '"q"' in body

    def test_populates_status_checkboxes(self, js_source: str) -> None:
        """Status is a multi-value param. The handler must walk all
        ``input[name=status]`` checkboxes and check/uncheck based on
        which values are in the URL."""
        idx = js_source.find("popstate")
        assert idx != -1
        body = js_source[idx : idx + 2000]
        assert "name=status" in body or "name='status'" in body or '"status"' in body

    def test_does_not_double_replacestate_inside_popstate(self, js_source: str) -> None:
        """Regression guard: when popstate re-fires the HTMX submit,
        the resulting afterRequest must NOT call replaceState again
        (the URL is already what popstate set). Without a guard, the
        forward-history would be clobbered. Pin that there's some
        sort of "skip replaceState on popstate-triggered submits"
        marker — implementation flag name is up to the implementer,
        but it must exist."""
        # The implementation uses a temporary flag (e.g. _popstateInFlight,
        # _skipReplaceState, _urlSyncSkip). We pin the contract loosely:
        # SOME guard reference must exist in the popstate handler OR the
        # afterRequest handler — they have to coordinate.
        idx_pop = js_source.find("popstate")
        idx_after = js_source.find("htmx:afterRequest")
        assert idx_pop != -1 and idx_after != -1
        window = js_source[min(idx_pop, idx_after) : max(idx_pop, idx_after) + 2500]
        # Loose pin: any of the flag-name patterns the implementer
        # might use. If a refactor changes the name, this test will
        # need updating — that's fine, it's a contract pin.
        guards = (
            "popstateInFlight",
            "_skipReplace",
            "_urlSync",
            "fromPopstate",
            "popState",
        )
        assert any(g in window for g in guards), (
            "Popstate + afterRequest handlers must coordinate so "
            "the popstate-triggered submit doesn't clobber the URL "
            "(double replaceState). Add a flag like _popstateInFlight."
        )


class TestPopstateRestoresRailOpenState:
    """#390: when popstate fires on a URL whose rail params are
    populated (repo / doc_type / owner / last_reviewed_after / tags),
    the filter-rail ``<details>`` must open. Without this, a user
    navigating back to a URL with active filters sees a closed rail
    that hides the filters they're presumably interested in.

    Status doesn't count for rail-open — the status pills live
    outside the rail. Mirrors the server-side
    :func:`rail_filters_active_count` semantics.
    """

    @staticmethod
    def _popstate_body(source: str) -> str:
        idx = source.find('addEventListener("popstate"')
        if idx == -1:
            idx = source.find("addEventListener('popstate'")
        assert idx != -1, "popstate listener must be registered"
        return source[idx : idx + 3000]

    def test_handler_targets_filter_rail(self, js_source: str) -> None:
        body = self._popstate_body(js_source)
        # The rail container is ``details.filter-rail`` (search.html:108).
        assert "filter-rail" in body, (
            "popstate handler must locate the .filter-rail <details> "
            "element to toggle its open state."
        )

    def test_handler_toggles_open_attribute(self, js_source: str) -> None:
        """The handler manipulates the ``open`` attribute. Either
        ``setAttribute("open", ...)`` / ``removeAttribute("open")``,
        or ``rail.open = ...`` is acceptable — pin both."""
        body = self._popstate_body(js_source)
        opens_via_attr = 'setAttribute("open"' in body and 'removeAttribute("open"' in body
        opens_via_property = ".open = " in body
        assert opens_via_attr or opens_via_property, (
            "popstate must toggle the rail <details> open state; "
            "without it, a navigation back to a URL with rail "
            "filters hides those filters behind a closed rail."
        )

    def test_status_alone_does_not_open_rail(self, js_source: str) -> None:
        """Status pills live OUTSIDE the rail (search.html:42-87). A
        URL with only ``?status=deprecated`` and no rail params must
        NOT open the rail — that would surface chrome for filters
        the user didn't set. The implementation must consult the
        five rail field names (NOT ``status``).

        Pin that the rail-open decision references at least the
        rail-field names that exist server-side
        (api.views._RAIL_FIELDS)."""
        body = self._popstate_body(js_source)
        # Pin two unambiguous rail-field names. ``repo`` alone could
        # ambiguously match the form field name handling, so also
        # pin ``last_reviewed_after`` which only appears in rail logic.
        assert "last_reviewed_after" in body, (
            "rail-open determination must consult the rail-field params (not just status)."
        )
