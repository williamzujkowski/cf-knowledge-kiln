/* #123 empty/edge-state polish for cf-knowledge-kiln.
 *
 * - Wires exemplar-query buttons in the empty state to populate
 *   #query and trigger the existing HTMX search form.
 * - Maintains a localStorage list of recent queries (max 10) and
 *   renders them into the <datalist id="recent-queries"> the input
 *   references via list="recent-queries".
 * - Hides the shortcut hint banner once the user has used any
 *   shortcut (kiln-keys.js sets the flag; we read it).
 *
 * Vanilla, no deps. Loaded with defer.
 */

(function () {
  "use strict";

  // localStorage key names — not secrets, despite the entropy.
  const RECENT_KEY = "kiln.recent-queries.v1"; // gitleaks:allow
  const HINT_KEY = "kiln.shortcut-hint-dismissed.v1"; // gitleaks:allow
  const ONBOARDING_KEY = "kiln.onboarding-seen.v1"; // gitleaks:allow
  const RECENT_MAX = 10;

  // ─── Recent queries (localStorage-backed) ─────────────────────────

  const loadRecent = () => {
    try {
      const raw = localStorage.getItem(RECENT_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.filter((s) => typeof s === "string") : [];
    } catch (_) {
      return [];
    }
  };

  const saveRecent = (list) => {
    try {
      localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
    } catch (_) {
      // Private-browsing or quota errors — ignore. The feature is best-effort.
    }
  };

  const pushRecent = (query) => {
    const q = (query || "").trim();
    if (!q) return;
    const list = loadRecent().filter((s) => s !== q);
    list.unshift(q);
    saveRecent(list);
    renderRecent();
  };

  const renderRecent = () => {
    const datalist = document.getElementById("recent-queries");
    if (!datalist) return;
    // DOM-property assignment (opt.value = q) is XSS-safe regardless of
    // the string contents. The previous innerHTML approach with manual
    // " → &quot; escaping was vulnerable to entity-decode round-trip:
    // a literal "&quot;" string in localStorage would be re-decoded by
    // the HTML parser on render, breaking out of the attribute and
    // injecting markup. (#137 reviewer MED.)
    datalist.replaceChildren(
      ...loadRecent().map((q) => {
        const opt = document.createElement("option");
        opt.value = q;
        return opt;
      })
    );
  };

  // ─── Exemplar buttons (empty state) ───────────────────────────────

  const onExemplarClick = (button) => {
    const q = button.getAttribute("data-exemplar-query");
    if (!q) return;
    const input = document.getElementById("query");
    if (!input) return;
    input.value = q;
    input.focus();
    // Trigger HTMX submit via the form's submit listener (configured on
    // hx-trigger="submit"). requestSubmit() respects validation + submitter.
    const form = input.closest("form");
    if (form && typeof form.requestSubmit === "function") {
      form.requestSubmit();
    }
  };

  document.addEventListener("click", (e) => {
    const target = e.target;
    if (target && target.classList && target.classList.contains("empty-exemplar")) {
      onExemplarClick(target);
    }
  });

  // ─── Shortcut hint banner ─────────────────────────────────────────

  const hideHint = () => {
    const hint = document.getElementById("shortcut-hint");
    if (hint) hint.setAttribute("data-dismissed", "true");
  };

  const showHintIfFresh = () => {
    try {
      if (localStorage.getItem(HINT_KEY) === "1") {
        hideHint();
      }
    } catch (_) {
      // ignore
    }
  };

  // Listen for any keyboard shortcut firing. kiln-keys.js handles the
  // actual shortcuts; we just observe that one fired (any of `/`, `j`,
  // `k`, `Enter` on a card, `c`, `o`, `?`). Listening on keydown with
  // a coarse filter covers the lot without coupling to kiln-keys.js.
  const SHORTCUT_KEYS = new Set(["/", "j", "k", "?", "c", "o"]);
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const target = e.target;
    const tag = target && target.tagName;
    // Don't dismiss the hint when the user is just typing in an input —
    // we want them to actually have used a shortcut outside of typing.
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (!SHORTCUT_KEYS.has(e.key)) return;
    try {
      localStorage.setItem(HINT_KEY, "1");
    } catch (_) {
      // ignore
    }
    hideHint();
  });

  // ─── Track submitted queries to push into recent ─────────────────

  // Listen for the search form submit (HTMX dispatches htmx:beforeRequest
  // before sending; we tap that to grab the current query). We only want
  // to push on actual submits (Enter / click), not on debounced keyup —
  // kiln-keys.js wires X-Kiln-Source: keyup-debounce for keyup-triggered
  // requests, so we filter on the triggering event type instead.
  document.addEventListener("htmx:beforeRequest", (e) => {
    const form = e.target;
    if (!form || !form.classList || !form.classList.contains("search-form")) return;
    const trig = e.detail.requestConfig && e.detail.requestConfig.triggeringEvent;
    if (trig && trig.type === "keyup") return;
    const input = form.querySelector('input[name="query"]');
    if (input) pushRecent(input.value);
  });

  // ─── #338 first-visit onboarding overlay ─────────────────────────

  // Dismiss + persist the localStorage flag in one place so all three
  // close paths (Skip, Try, Esc, backdrop) end the same way.
  const dismissOnboarding = () => {
    const dlg = document.getElementById("onboarding");
    if (!dlg) return;
    try {
      localStorage.setItem(ONBOARDING_KEY, "1");
    } catch (_) {
      // Quota / private-browsing — best-effort, the dialog still closes.
    }
    if (typeof dlg.close === "function") {
      dlg.close();
    }
    dlg.setAttribute("data-hidden", "true");
  };

  // Show the overlay on first visit only. Suppressed when:
  //   * The localStorage flag is set (the user has dismissed it before)
  //   * prefers-reduced-motion: reduce — the scrim fade is the motion
  //     we'd otherwise show; respect the OS preference by skipping
  //     entirely. The exemplars in the empty state remain visible.
  //   * The native <dialog> element + showModal isn't available — we
  //     fall back to skipping (the exemplars cover the JS-off path).
  const showOnboardingIfFresh = () => {
    const dlg = document.getElementById("onboarding");
    if (!dlg) return;
    if (typeof dlg.showModal !== "function") return;
    let seen;
    try {
      seen = localStorage.getItem(ONBOARDING_KEY) === "1";
    } catch (_) {
      seen = false;
    }
    if (seen) return;
    if (
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      // Persist the flag so a single-show user doesn't see it later if
      // they un-set the preference. Conservative; could be revisited.
      try {
        localStorage.setItem(ONBOARDING_KEY, "1");
      } catch (_) {
        // ignore
      }
      return;
    }
    dlg.removeAttribute("data-hidden");
    dlg.showModal();
  };

  // Delegated click for the two onboarding buttons. Skip → close +
  // persist. Try → close + focus the search input + scroll the
  // first exemplar into view so the user has something concrete to
  // click next.
  document.addEventListener("click", (e) => {
    const target = e.target;
    if (!target || !target.getAttribute) return;
    const action = target.getAttribute("data-action");
    if (action === "onboarding-skip") {
      dismissOnboarding();
      return;
    }
    if (action === "onboarding-try") {
      dismissOnboarding();
      const input = document.getElementById("query");
      if (input) {
        input.focus();
        // Don't scroll the input — autofocus already did that. Scroll
        // the exemplars list into view so the user sees the next
        // concrete affordance.
        const exemplars = document.querySelector(".empty-exemplars");
        if (exemplars && typeof exemplars.scrollIntoView === "function") {
          exemplars.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }
    }
  });

  // ─── Init on page ready ───────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", () => {
    renderRecent();
    showHintIfFresh();
    showOnboardingIfFresh();
  });
})();
