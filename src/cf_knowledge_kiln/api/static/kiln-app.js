/* #131 application lifecycle JS for cf-knowledge-kiln.
 *
 * Extracted from an inline <script> block in base.html so the UI can
 * later run under `Content-Security-Policy: script-src 'self'` (no
 * `unsafe-inline`). No build step, no dependencies. Behavior must
 * match the previous inline implementation exactly — the only
 * change is that two former inline `onclick` handlers are now
 * routed through `data-action` delegation:
 *
 *   data-action="open-preview"   on .result-title-button
 *     → sets data-open on #preview so the mobile drawer slides in.
 *   data-action="close-preview"  on .preview-close
 *     → clears #preview state + innerHTML.
 *
 * Wires (all identical to the prior base.html block):
 *   htmx:beforeRequest    flip #results aria-busy + announce "Searching…"
 *   htmx:afterSwap        resting-state announcement + mobile drawer focus
 *   htmx:configRequest    propagate keyup-debounce header (#120 telemetry)
 *   htmx:beforeSwap       allow swaps for 400/404/429/503 (rendered fragments)
 *   keydown(Esc)          close preview
 *   click(#preview-backdrop, [data-cheatsheet-close], [data-action=*])
 */

(function () {
  "use strict";

  // a11y (#120): announcements live on #search-status, NOT on
  // #results. The results region only carries aria-busy so AT can
  // hint "this is loading". The status text is the only thing the
  // screen reader speaks — "Searching…", then the new count.
  const _setStatus = (text) => {
    const s = document.getElementById("search-status");
    if (s) s.textContent = text;
  };

  // #321: feedback ACK announcements got their own live region so
  // a feedback vote landing milliseconds after a preview-load (or a
  // search-count update) can't overwrite the prior announcement in
  // #search-status. Two independent polite regions can't collide
  // because AT processes each one independently. Falls back to
  // #search-status when the dedicated region isn't present (so the
  // JS works against an unmigrated template).
  const _setFeedbackStatus = (text) => {
    const s =
      document.getElementById("feedback-status") ||
      document.getElementById("search-status");
    if (s) s.textContent = text;
  };

  document.addEventListener("htmx:beforeRequest", (e) => {
    const r = document.getElementById("results");
    if (r && e.target.closest("form.search-form")) {
      r.setAttribute("aria-busy", "true");
      _setStatus("Searching…");
    }
  });

  document.addEventListener("htmx:afterSwap", (e) => {
    const r = document.getElementById("results");
    if (r && e.detail.target && e.detail.target.id === "results") {
      r.setAttribute("aria-busy", "false");
      const count = r.querySelector(".results-count .count");
      if (count) {
        const n = parseInt(count.textContent, 10);
        _setStatus(n === 1 ? "1 result" : n + " results");
      } else if (r.querySelector(".empty-results")) {
        _setStatus("No results");
      } else if (r.querySelector(".error-fragment")) {
        // #132 reviewer MED: error swaps left status stuck on
        // "Searching…". The error fragment itself carries
        // role="alert" so AT does announce it, but the status
        // region must surface the resting state too.
        _setStatus("Search unavailable");
      } else {
        _setStatus("");
      }
    }
    // #314 fix-2: preview swap AT announcement. Desktop-rail users
    // had no AT signal on preview load; mobile got panel.focus()
    // but desktop got nothing. Route the announcement through the
    // same #search-status live region used for search counts —
    // single live region avoids the textbook double-announce trap.
    const panel = e.detail.target;
    if (panel && panel.id === "preview") {
      const title = panel.querySelector(".preview-title")?.textContent?.trim();
      const missing = panel.querySelector(".preview-missing");
      if (missing) {
        _setStatus("Preview unavailable");
      } else if (title) {
        _setStatus("Preview loaded: " + title);
      }
    }
    // Mobile drawer focus (#120 / #130 review): only on closed→open
    // transition. Re-grabbing focus on every chunk-swap was a UX
    // trap — repeated preview-card clicks kept yanking focus from
    // wherever the user had tabbed to (#132 reviewer MED).
    if (
      panel &&
      panel.id === "preview" &&
      panel.hasAttribute("data-open") &&
      !panel.hasAttribute("data-focus-grabbed") &&
      window.matchMedia("(max-width: 959px)").matches
    ) {
      panel.setAttribute("data-focus-grabbed", "true");
      panel.focus();
    }
    // #314 fix-1: feedback ack AT announcement. The swapped ack
    // fragment used to carry role="status" but that doesn't fire
    // reliably for live regions born WITH the role at swap-time;
    // route through the persistent #feedback-status region instead
    // (#321: previously #search-status, which collided with preview
    // + search-count announcements when both fired in the same
    // event tick). matches?() is the HTMX 2.x-friendly null-safe
    // check.
    const target = e.detail.target;
    if (target && target.matches?.("[data-feedback-ack]")) {
      const sig = target.getAttribute("data-signal") || "feedback";
      _setFeedbackStatus("Feedback recorded: " + sig.replace(/_/g, " "));
    }
  });

  // #120 telemetry gating: keyup-debounced searches add an
  // X-Kiln-Source header so the server can skip the rag_queries
  // row write. Only explicit submits persist telemetry. HTMX 2.x
  // exposes the originating event on detail.triggeringEvent.
  document.addEventListener("htmx:configRequest", (e) => {
    const ev = e.detail.triggeringEvent;
    if (ev && ev.type === "keyup") {
      e.detail.headers["X-Kiln-Source"] = "keyup-debounce";
    }
  });

  // #371 URL-shareable filter state (replaceState on submit +
  // popstate handler) lives in kiln-url-state.js — extracted in
  // #394 to keep this file close to the 400-line AGENTS cap.

  // HTMX 2.x drops 4xx/5xx swaps by default. We deliberately render
  // a swap-friendly fragment on 429 (rate limit) and 503 (retrieval
  // outage) — let those statuses swap so the user sees the inline
  // error instead of a silent no-op. 404 covers the preview panel's
  // not-found fragment. Status comes back via detail.xhr.status
  // because HX-Retarget on the server is too coarse here.
  // #293: widened from {400, 404, 429, 503} to include 500, 502,
  // 504. A 5xx from a sluggish DB or transient upstream failure
  // was being swallowed silently by HTMX's default isError
  // behavior, leaving the user with no visible signal of the
  // failure (and no chance to retry intentionally vs reflexively).
  // The server-side error fragments now render uniformly across
  // the 4xx + 5xx range.
  document.addEventListener("htmx:beforeSwap", (e) => {
    const s = e.detail.xhr.status;
    if (
      s === 400 ||
      s === 404 ||
      s === 429 ||
      s === 500 ||
      s === 502 ||
      s === 503 ||
      s === 504
    ) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });

  // #119 preview panel: Esc clears it (mobile drawer + desktop rail).
  //
  // #345: focus return. Before closing, capture the previously-
  // focused element (the result-card title button that the user came
  // from) and restore focus to it after the panel clears. Without
  // this, Esc-close lands focus on <body> — keyboard users lose
  // their place in the result list and have to Tab from the top of
  // the page back to where they were.
  const _closePreview = () => {
    const p = document.getElementById("preview");
    if (!p || !p.hasAttribute("data-open")) return;
    // Capture the "open from" element BEFORE we clear data-open
    // (because the captured ref is stored on the panel itself for
    // both Esc and click-backdrop paths).
    const opener = p.dataset.openerKey
      ? document.querySelector(
          '.result-title-button[data-chunk-id="' + p.dataset.openerKey + '"]'
        )
      : null;
    p.removeAttribute("data-open");
    // Allow focus-grab again next time the drawer reopens.
    p.removeAttribute("data-focus-grabbed");
    delete p.dataset.openerKey;
    // #346: drop the mobile-drawer modal semantics so the next open
    // on a desktop viewport (orientation flip, dev-tools resize)
    // doesn't inherit a stale role=dialog on the sticky rail.
    p.removeAttribute("role");
    p.removeAttribute("aria-modal");
    p.innerHTML = "";
    if (opener && typeof opener.focus === "function") {
      // Inside a requestAnimationFrame so the focus lands AFTER the
      // browser commits the data-open removal (otherwise the panel
      // can still be the focus target during the same paint frame).
      requestAnimationFrame(() => opener.focus());
    }
  };

  // Open the preview drawer (replaces the prior inline onclick on
  // .result-title-button). Only flips the data-open attribute —
  // HTMX still drives the actual content fetch through hx-get.
  //
  // #345: capture the opener's chunk-id on the panel so _closePreview
  // can restore focus to the originating card. The opener element
  // itself isn't stored (DOM refs go stale across HTMX swaps); we
  // re-query by chunk-id at close time so the result-card the user
  // came from gets focus even if results were re-rendered.
  //
  // #346: on a mobile viewport the panel is presenting as a drawer
  // that visually obscures the page. Set role=dialog + aria-modal so
  // AT announces it as a modal; the Tab trap below loops focus inside
  // the panel for the sighted-keyboard user. Desktop sticky rail must
  // NOT carry these — it's a panel, not a modal — so the assignment
  // is gated on the same matchMedia query the focus-grab uses (#120).
  const _openPreview = (opener) => {
    const p = document.getElementById("preview");
    if (!p) return;
    p.setAttribute("data-open", "true");
    if (opener && opener.dataset && opener.dataset.chunkId) {
      p.dataset.openerKey = opener.dataset.chunkId;
    } else {
      delete p.dataset.openerKey;
    }
    if (window.matchMedia("(max-width: 959px)").matches) {
      p.setAttribute("role", "dialog");
      p.setAttribute("aria-modal", "true");
    }
  };

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") _closePreview();
  });

  // #346 mobile drawer focus trap. While the preview drawer is open
  // on a (max-width: 959px) viewport, Tab + Shift+Tab loop focus
  // within the panel instead of walking out into the obscured page
  // beneath the backdrop. Pattern mirrors the cheatsheet trap in
  // kiln-keys.js:188-198, generalized for N focusables (the cheatsheet
  // only has one — the close button).
  //
  // Desktop: bails out at the matchMedia check so the sticky rail is
  // a regular page region. Drawer closed: bails on the data-open
  // check — no trap when the panel isn't presenting.
  //
  // Focusable selector covers what _preview.html actually renders:
  // the close <button>, the canonical-source <a>, and the
  // prev/next <summary> rows. Hidden / disabled elements are
  // filtered out via the :not(...) selectors so the trap doesn't
  // land focus on something the user can't see.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const p = document.getElementById("preview");
    if (!p || !p.hasAttribute("data-open")) return;
    if (!window.matchMedia("(max-width: 959px)").matches) return;
    const focusables = Array.from(
      p.querySelectorAll(
        'button:not([disabled]), [href], summary, ' +
          '[tabindex]:not([tabindex="-1"])'
      )
    ).filter((el) => el.offsetParent !== null);
    if (focusables.length === 0) {
      // Degenerate: nothing focusable inside (e.g. preview-missing
      // fragment). Keep focus on the panel itself so the user can
      // still Esc out without Tab dropping them onto the masthead.
      e.preventDefault();
      p.focus();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    // Focus outside the panel (race condition — should not happen
    // once the focus-grab on open has fired, but guard anyway):
    // pull it back to the first focusable.
    if (!p.contains(active)) {
      e.preventDefault();
      first.focus();
      return;
    }
    // Focus on the panel itself (the focus-grab landing state):
    // forward Tab → first focusable, Shift+Tab → last. Without the
    // panel-itself carve-out on BOTH sides, the loop is asymmetric
    // and either direction lets the user Tab out into the obscured
    // page underneath the drawer.
    if (e.shiftKey && (active === first || active === p)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || active === p)) {
      e.preventDefault();
      first.focus();
    }
  });

  // #346 viewport-change cleanup. If the user opens the drawer on a
  // mobile viewport (role=dialog + aria-modal applied), then rotates
  // the device or resizes past 960px without closing, the modal
  // semantics would otherwise persist on what is now a desktop
  // sticky rail — and AT would announce a "dialog" that isn't
  // presenting as one. Listen on the same matchMedia query and
  // strip the stale attributes when we cross the threshold while
  // the drawer is open. (The Tab trap is already viewport-gated, so
  // it stops firing automatically — only the ARIA attrs need this
  // cleanup.)
  const _mobileMq = window.matchMedia("(max-width: 959px)");
  const _onViewportChange = (mq) => {
    if (mq.matches) return;
    const p = document.getElementById("preview");
    if (!p || !p.hasAttribute("data-open")) return;
    p.removeAttribute("role");
    p.removeAttribute("aria-modal");
  };
  if (typeof _mobileMq.addEventListener === "function") {
    _mobileMq.addEventListener("change", _onViewportChange);
  } else if (typeof _mobileMq.addListener === "function") {
    // Safari < 14 fallback. Drop when min target moves past 14.
    _mobileMq.addListener(_onViewportChange);
  }

  // Single delegated click listener. Order of checks matches the
  // prior inline implementation: backdrop-close → cheatsheet-close
  // → data-action dispatch. Each branch is independent.
  document.addEventListener("click", (e) => {
    const target = e.target;
    if (!target) return;

    // Backdrop click closes the drawer on mobile (#120 / #130 review).
    if (target.id === "preview-backdrop") {
      _closePreview();
      return;
    }

    // #121 cheatsheet close button (kiln-keys.js handles `?` to open).
    if (target.hasAttribute && target.hasAttribute("data-cheatsheet-close")) {
      const sheet = document.getElementById("cheatsheet");
      if (sheet) sheet.removeAttribute("data-open");
      return;
    }

    // #131 data-action dispatch. The button itself may not be the
    // direct event target (an inner <span> often is), so walk up to
    // the nearest [data-action] ancestor.
    const actor =
      target.closest && target.closest("[data-action]")
        ? target.closest("[data-action]")
        : null;
    if (!actor) return;
    const action = actor.getAttribute("data-action");
    if (action === "open-preview") {
      // Pass the activated control (the result-title button) so
      // _openPreview can record it for focus-return on close (#345).
      _openPreview(actor);
    } else if (action === "close-preview") {
      _closePreview();
    } else if (action === "reset-filters") {
      // #347: native <input type=reset> already cleared the form
      // by the time this click handler fires; re-fire the HTMX
      // submit on the search form so the cleared filter state
      // round-trips through POST /search and the result list
      // updates. Without this the form sits in its defaults but
      // the user still sees the prior filtered results.
      //
      // Use HTMX's programmatic trigger if HTMX is loaded;
      // fall back to dispatching a regular submit event so the
      // form's native handler runs even without HTMX (graceful
      // degradation per AGENTS.md).
      const form = actor.closest("form.search-form");
      if (!form) return;
      // requestAnimationFrame so the reset happens FIRST (the
      // browser applies it on this same tick), and the re-submit
      // sees the cleared state.
      requestAnimationFrame(() => {
        if (window.htmx && typeof window.htmx.trigger === "function") {
          window.htmx.trigger(form, "submit");
        } else {
          form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        }
      });
    }
  });
})();
