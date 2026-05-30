/* #371 URL-shareable filter state on /search.
 *
 * Extracted from kiln-app.js (issue #394) so each file stays close
 * to the 400-line AGENTS soft cap. The handlers here are
 * self-contained — they don't share helpers with the rest of
 * kiln-app.js — so the split is a straight code-move, no behavior
 * change.
 *
 * Two pieces:
 *
 * 1. htmx:afterRequest listener gated on form.search-form:
 *    serialise the form to URLSearchParams → call
 *    history.replaceState. ``replaceState`` (not ``pushState``)
 *    so 300ms-debounced keystrokes don't pollute the back-button
 *    stack with one entry per character.
 *
 * 2. popstate listener: on browser back/forward, re-populate the
 *    form fields from the URL and re-fire the HTMX submit so the
 *    page matches what the URL says. ``_popstateInFlight`` flag
 *    prevents the resulting afterRequest from re-writing the URL
 *    (which would clobber the forward-history entry).
 *
 * The two listeners coordinate via the shared closure-private
 * _popstateInFlight flag. They're inside a single IIFE rather
 * than the kiln-app.js IIFE so that flag stays scoped to URL-
 * state work and can't be accidentally touched by other handlers.
 */

(function () {
  "use strict";

  let _popstateInFlight = false;

  document.addEventListener("htmx:afterRequest", (e) => {
    if (_popstateInFlight) {
      _popstateInFlight = false;
      return;
    }
    const form = e.target && e.target.closest && e.target.closest("form.search-form");
    if (!form) return;
    if (!e.detail || !e.detail.successful) return;
    const params = new URLSearchParams();
    // FormData walks the form, including multi-checked inputs.
    // Translate ``query`` (form name) to ``q`` (URL param) since
    // the GET /search route expects ``q`` — matches the natural
    // short URL convention and the route signature.
    const data = new FormData(form);
    for (const [k, v] of data.entries()) {
      if (k === "_filters_set") continue; // server-internal marker
      if (typeof v !== "string") continue;
      if (k === "query") {
        if (v) params.append("q", v);
      } else if (v !== "") {
        params.append(k, v);
      }
    }
    const next = params.toString() ? `/search?${params.toString()}` : "/search";
    history.replaceState(null, "", next);
  });

  window.addEventListener("popstate", () => {
    const form = document.querySelector("form.search-form");
    if (!form) return;
    const params = new URLSearchParams(window.location.search);
    // Reset multi-value status checkboxes to URL state. If no
    // status params present at all → leave defaults; otherwise the
    // URL is canonical.
    const statusBoxes = form.querySelectorAll("input[name=status]");
    const statusVals = params.getAll("status");
    if (statusVals.length > 0) {
      statusBoxes.forEach((cb) => {
        cb.checked = statusVals.includes(cb.value);
      });
    }
    // doc_type same shape
    const docTypeBoxes = form.querySelectorAll("input[name=doc_type]");
    const docTypeVals = params.getAll("doc_type");
    if (docTypeVals.length > 0) {
      docTypeBoxes.forEach((cb) => {
        cb.checked = docTypeVals.includes(cb.value);
      });
    }
    // Single-value text/date fields. URL param ``q`` → form name
    // ``query`` (single mapping); everything else is the same name.
    const q = form.querySelector("input[name=query]");
    if (q) q.value = params.get("q") || "";
    for (const name of ["repo", "owner", "last_reviewed_after", "tags"]) {
      const el = form.querySelector(`input[name=${name}]`);
      if (el) el.value = params.get(name) || "";
    }
    // Re-fire the HTMX submit so the results match the URL.
    _popstateInFlight = true;
    if (window.htmx && typeof window.htmx.trigger === "function") {
      window.htmx.trigger(form, "submit");
    } else {
      form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    }
  });
})();
