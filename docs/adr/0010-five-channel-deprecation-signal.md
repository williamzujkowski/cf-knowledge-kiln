---
id: ADR-0010
title: Five-channel deprecation signal for result-card status flagging
status: accepted
date: 2026-05-26
deciders: william
supersedes: null
superseded_by: null
---

## Context

`docs/user-journeys.md:47-57` ratifies the load-bearing UX rule:

> Deprecated/archived/superseded results may appear but **must** be
> visually flagged. Showing a deprecated doc as if it were current is
> a bug, not a feature.

A frontend audit in mid-May 2026 flagged the pre-#271 result card as
too subtle. Three failure modes:

1. **Color-blind users miss the cue.** The pre-#271 treatment used a
   pale oxblood-stripe pattern and a single-step `--ink → --ink-soft`
   shift on the title. Color-blind users with deuteranomaly or
   protanomaly see both `--ink` and `--ink-soft` as the same gray;
   the stripe pattern flattens to near-invisible diagonal lines on
   `--paper-dim`.
2. **Scan-speed users miss the cue.** A security engineer triaging
   20 cards at ~200ms each doesn't read the small-caps status
   word ("DEPRECATED") — they're scanning shapes and tone. A
   single-channel signal needs ~600ms of focused attention to
   register; that's three card-scans late.
3. **Mobile users lose the only remaining cue.** Without a layered
   signal, the only channel that survives the 640px breakpoint's
   typographic compression is the small-caps status badge — which
   sits inside the header alongside the (also small-caps) title's
   neighbors. Visual hierarchy collapses.

The PRs that fixed this (#271 stamp + base treatment, #277 mobile
preservation, #281 a11y-dedupe with status-badge tooltip) collectively
codified a **five-channel layered signal**. The decision now lives
across three PR descriptions, the CSS partials, and a half-dozen
pinning tests — but with no single doc that names the principle or
the rules a future status (e.g. `withdrawn`, `recalled`) would have
to obey.

## Decision

Adopt a **five-channel layered signal** for result cards whose
status is `deprecated`, `archived`, or `superseded`. Each channel
encodes the "this is not current" message in a perceptually
independent way, so no single sensory failure (color-blindness,
fast scanning, mobile compression, JS disabled, screen-reader only,
print, dark mode) can drop the message entirely.

### The five channels

| # | Channel | CSS hook | Behavior |
|---|---|---|---|
| 1 | **Verbal stamp** | `.deprecation-stamp` + `.deprecation-stamp-text` | Editorial small-caps copy in the header: `Deprecated · do not cite`, `Archived · historical reference`, `Superseded · see successor`. Renders even with CSS-disabled browsers; reads at scan speed. |
| 2 | **Striped body** | `.result-card.status-{deprecated,archived,superseded}` `background-image` | Dense diagonal `--oxblood-stripe` pattern (8px spacing). Conveys "this is set aside" without relying on color saturation. |
| 3 | **Title strikethrough** | `.result-card.status-{...}` `.result-title { text-decoration: line-through }` | Direct visual cancellation of the most-scanned element. Unambiguous in print, in dark mode, in print preview. |
| 4 | **Heavy gutter rule** | `.result-card.status-{...}::before` + `::after` | 3px oxblood `::before` running the full inline-start edge of the card, with a 1rem perpendicular cap via `::after`. Reads as an editorial margin mark. Narrows to 2px on mobile (`#277`) to fit the 1.75rem gutter without dominating. |
| 5 | **Body opacity fade** | `.result-card.status-{...}` `.excerpt`, `.result-footer { opacity: 0.85 }` | Quiet de-emphasis of body content. Subtle on its own; load-bearing when the other channels are visible. |

### Hard rules

1. **Adding a new deprecated-class status MUST emit all five channels.** A status that opts into the `--ink-soft → --oxblood` color treatment without also setting the gutter rule, stripe, strikethrough, stamp, and body fade is a half-flag — exactly the failure mode pre-#271. Add the status to `_DEPRECATION_LABELS` (channel 1) AND extend the CSS selectors for channels 2-5 in the same PR.
2. **No channel may be removed without removing the entire treatment.** A "minimalist refactor" that drops the gutter rule because "it duplicates the stripe" reduces the layered signal to four and loses the print + dark-mode + color-blind survival of that channel. The pinning tests under `tests/integration/test_results_mobile_css.py` and `tests/unit/test_status_badge_template.py` exist to catch this.
3. **Channel 1 (verbal stamp) MUST carry the AT announcement.** The status badge's `aria-label` (added in #281) already covers the AT path for the status word; the stamp is `aria-hidden="true"` to prevent double-announcement. A future status that doesn't have a corresponding `_STATUS_TOOLTIPS` entry breaks this — the badge falls back to no tooltip, the stamp is hidden from AT, and screen-reader users hear nothing. The TestStatusTooltip suite enforces the table.
4. **Mobile channels narrow, they do not drop.** PR #277 narrowed the gutter rule from 3px → 2px and tightened the stamp's `letter-spacing` from 0.16em → 0.10em at the worst-case 360px breakpoint. The five channels remain present; they just consume less inline space.
5. **The `active` / `approved` / `draft` statuses MUST NOT emit any of the five channels.** The absence of the treatment is the signal that the card is current. The `_DEPRECATION_LABELS.get(status)` pattern returns `None` for current statuses so the template `{% if r.deprecation_label %}` cleanly suppresses the stamp; the CSS selectors are scoped to the three deprecated-class statuses by name (not by negation) so a new current-class status (`approved-with-notes`?) doesn't accidentally inherit the treatment.

### What "five" buys us

The signals are **perceptually independent** — each survives a different sensory failure:

| Failure mode | Channels that survive |
|---|---|
| Color-blind (red-green) | Stamp text, strikethrough, gutter-rule shape, body-fade opacity, stripe pattern (shape, not color) |
| Fast scanning (200ms/card) | Stripe (peripheral vision), heavy gutter rule (vertical anchor), strikethrough |
| Screen reader only | Stamp via status-badge aria-label (channel 1 by proxy) |
| CSS disabled | Stamp text (channel 1 — survives the whole stylesheet failure) |
| Print | Stamp, strikethrough, gutter rule (printed as a black bar) |
| Dark mode | All five (tokens flip via prefers-color-scheme) |
| Mobile ≤640px | All five (narrowed but present) |
| Mobile ≤360px | Four (heading-path display:none for vertical budget; the five-channel deprecation signal is preserved) |

## Consequences

### Positive

- One canonical reference for the principle. Future UX work and design reviews can cite this ADR instead of three PR descriptions.
- A predictable extension contract for new statuses. A `withdrawn` status added in a future ADR knows exactly what CSS hooks and template changes to add.
- A test contract for what "intact" means — `tests/integration/test_results_mobile_css.py::test_deprecation_stamp_owns_its_row_on_mobile` and the related pin-tests are the executable spec.

### Negative

- Higher per-card CSS budget. Each deprecated card carries `::before`, `::after`, a background-image stripe, a strikethrough, an opacity shift, plus the stamp markup. Profile shows ~0.4ms additional layout per card; negligible on a 20-card page.
- Higher cognitive load on the design reviewer. A PR touching status presentation must check all five channels. The pinning tests are the safety net but a reviewer who only reads the visual diff can miss a regression on (e.g.) the printed gutter rule.

### Neutral

- The status-badge tooltip (added in #281) is **not** one of the five channels. It's a sixth signal that disambiguates the color-coded badge; it complements but doesn't duplicate the five.

## References

- PR [#271](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/271) — initial stamp + base five-channel treatment.
- PR [#277](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/277) — mobile preservation (channels narrow, don't drop).
- PR [#281](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/281) — a11y dedupe (stamp goes aria-hidden when status-badge tooltip carries the AT announcement).
- `docs/user-journeys.md:47-57` — the load-bearing UX rule.
- `src/cf_knowledge_kiln/api/views.py` `_DEPRECATION_LABELS` + `_STATUS_TOOLTIPS` — channel-1 / channel-6 copy.
- `src/cf_knowledge_kiln/api/static/kiln/_results.css` — channels 2-5 CSS hooks.
- `tests/integration/test_results_mobile_css.py` — mobile-preservation contract.
- `tests/unit/test_status_badge_template.py` — aria-hidden dedupe contract.
