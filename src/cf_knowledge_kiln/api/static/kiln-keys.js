/* #121 keyboard navigation for cf-knowledge-kiln.
 *
 * No build step, no dependencies, no inline handlers. Wires:
 *   /             focus #query (CLI-style, when not already in an input)
 *   j / ArrowDown next result card
 *   k / ArrowUp   previous result card
 *   Enter         on a focused card, open its preview panel
 *   c             on a focused card, copy "repo/path#heading" to clipboard
 *   o             on a focused card, toggle full chunk text
 *   ?             open the shortcut cheatsheet
 *   Esc           close cheatsheet (also closes preview via base.html)
 *
 * Roving tabindex: only one .result-card has tabindex=0 at a time. j/k
 * shift it and move focus. Reaches the cards even though they aren't
 * naturally focusable (<li>).
 *
 * Server renders each card with data attributes consumed here:
 *   data-card             selector hook
 *   data-chunk-id         preview hx-get target
 *   data-heading-path     joined heading_path for clipboard
 *   data-repo, data-path  the citation prefix
 */

(function () {
  "use strict";

  const isTypingInElement = (el) => {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  };

  const cards = () => Array.from(document.querySelectorAll(".result-card[data-card]"));

  const currentIndex = (list) => {
    const focused = document.activeElement;
    return list.findIndex((c) => c === focused);
  };

  const focusCard = (card) => {
    if (!card) return;
    cards().forEach((c) => c.setAttribute("tabindex", "-1"));
    card.setAttribute("tabindex", "0");
    card.focus();
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };

  const move = (delta) => {
    const list = cards();
    if (list.length === 0) return;
    const idx = currentIndex(list);
    if (idx === -1) {
      focusCard(list[0]);
      return;
    }
    const next = Math.max(0, Math.min(list.length - 1, idx + delta));
    focusCard(list[next]);
  };

  const focusQuery = () => {
    const q = document.getElementById("query");
    if (!q) return;
    q.focus();
    q.select();
  };

  const openPreview = (card) => {
    if (!card) return;
    const chunkId = card.getAttribute("data-chunk-id");
    if (!chunkId) return;
    // Use the existing hx-get button inside the card so HTMX handles
    // the request + swap exactly as a click would. Falls back to a
    // direct fetch if the markup ever changes.
    const trigger = card.querySelector(".result-title-button");
    if (trigger) {
      trigger.click();
    }
  };

  const showToast = (text) => {
    let toast = document.getElementById("toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "toast";
      toast.setAttribute("role", "status");
      toast.setAttribute("aria-live", "polite");
      document.body.appendChild(toast);
    }
    toast.textContent = text;
    toast.setAttribute("data-visible", "true");
    if (toast._timer) clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      toast.removeAttribute("data-visible");
    }, 1800);
  };

  const copyCitation = (card) => {
    if (!card) return;
    const repo = card.getAttribute("data-repo") || "";
    const path = card.getAttribute("data-path") || "";
    const heading = card.getAttribute("data-heading-path") || "";
    if (!repo || !path) {
      showToast("Citation unavailable");
      return;
    }
    const text = heading ? `${repo}/${path}#${heading}` : `${repo}/${path}`;
    // navigator.clipboard requires a secure context; in dev over http
    // we fall back to a hidden textarea + execCommand so the shortcut
    // still works in test fixtures.
    const write = navigator.clipboard && navigator.clipboard.writeText
      ? navigator.clipboard.writeText(text)
      : new Promise((resolve, reject) => {
          try {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            ta.remove();
            resolve();
          } catch (e) {
            reject(e);
          }
        });
    write.then(
      () => showToast("Citation copied"),
      () => showToast("Copy failed")
    );
  };

  const toggleExpand = (card) => {
    if (!card) return;
    const expanded = card.getAttribute("data-expanded") === "true";
    if (expanded) {
      card.removeAttribute("data-expanded");
    } else {
      card.setAttribute("data-expanded", "true");
    }
  };

  const toggleCheatsheet = (force) => {
    const sheet = document.getElementById("cheatsheet");
    if (!sheet) return;
    const isOpen = sheet.hasAttribute("data-open");
    const shouldOpen = typeof force === "boolean" ? force : !isOpen;
    if (shouldOpen) {
      sheet.setAttribute("data-open", "true");
      sheet.focus();
    } else {
      sheet.removeAttribute("data-open");
    }
  };

  const onKeydown = (e) => {
    // Modifier keys never trigger shortcuts — leave native bindings alone.
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    // Cheatsheet handling is the only set of shortcuts that fires
    // inside inputs (Esc closes it; ? in the input still types it).
    if (e.key === "Escape") {
      const sheet = document.getElementById("cheatsheet");
      if (sheet && sheet.hasAttribute("data-open")) {
        toggleCheatsheet(false);
        return;
      }
    }

    const typing = isTypingInElement(document.activeElement);

    // `?` is Shift+/ on US layouts. Allow it from outside inputs only.
    if (!typing && e.key === "?") {
      e.preventDefault();
      toggleCheatsheet();
      return;
    }

    // `/` focuses the search input — the universal CLI affordance.
    if (!typing && e.key === "/") {
      e.preventDefault();
      focusQuery();
      return;
    }

    if (typing) return;

    if (e.key === "j" || e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
      return;
    }
    if (e.key === "k" || e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
      return;
    }

    // Per-card shortcuts only fire when a card is focused.
    const focused = document.activeElement;
    if (!focused || !focused.classList || !focused.classList.contains("result-card")) {
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      openPreview(focused);
      return;
    }
    if (e.key === "c") {
      e.preventDefault();
      copyCitation(focused);
      return;
    }
    if (e.key === "o") {
      e.preventDefault();
      toggleExpand(focused);
      return;
    }
  };

  // Make freshly-swapped result cards focusable. The first card gets
  // tabindex=0 (entry point); the rest get tabindex=-1 (roving will
  // bump them when navigated).
  const seedRovingTabindex = () => {
    const list = cards();
    if (list.length === 0) return;
    list.forEach((c, i) => {
      c.setAttribute("tabindex", i === 0 ? "0" : "-1");
    });
  };

  document.addEventListener("keydown", onKeydown);
  document.addEventListener("DOMContentLoaded", seedRovingTabindex);
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target && e.detail.target.id === "results") {
      seedRovingTabindex();
    }
  });
})();
