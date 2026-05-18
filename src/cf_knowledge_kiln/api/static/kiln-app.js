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
    // Mobile drawer focus (#120 / #130 review): only on closed→open
    // transition. Re-grabbing focus on every chunk-swap was a UX
    // trap — repeated preview-card clicks kept yanking focus from
    // wherever the user had tabbed to (#132 reviewer MED).
    const panel = e.detail.target;
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

  // HTMX 2.x drops 4xx/5xx swaps by default. We deliberately render
  // a swap-friendly fragment on 429 (rate limit) and 503 (retrieval
  // outage) — let those statuses swap so the user sees the inline
  // error instead of a silent no-op. 404 covers the preview panel's
  // not-found fragment. Status comes back via detail.xhr.status
  // because HX-Retarget on the server is too coarse here.
  document.addEventListener("htmx:beforeSwap", (e) => {
    const s = e.detail.xhr.status;
    if (s === 429 || s === 503 || s === 400 || s === 404) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });

  // #119 preview panel: Esc clears it (mobile drawer + desktop rail).
  const _closePreview = () => {
    const p = document.getElementById("preview");
    if (!p || !p.hasAttribute("data-open")) return;
    p.removeAttribute("data-open");
    // Allow focus-grab again next time the drawer reopens.
    p.removeAttribute("data-focus-grabbed");
    p.innerHTML = "";
  };

  // Open the preview drawer (replaces the prior inline onclick on
  // .result-title-button). Only flips the data-open attribute —
  // HTMX still drives the actual content fetch through hx-get.
  const _openPreview = () => {
    const p = document.getElementById("preview");
    if (p) p.setAttribute("data-open", "true");
  };

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") _closePreview();
  });

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
      _openPreview();
    } else if (action === "close-preview") {
      _closePreview();
    }
  });
})();
