"""Mobile result-card legibility rules survive bundle regeneration (#276).

The desktop card grid (``var(--gutter) 1fr`` where ``--gutter:
2.75rem``) is uncomfortable below 640px and unusable at 320px. The
audit (epic #268) flagged this; PR #276 fixes it with two
``@media`` blocks in ``_results.css``. These tests assert the
responsive contract at the bundle level so a future cleanup that
re-bundles ``kiln.css`` without the partial trips immediately,
before users see a regression.

Pattern matches the existing ``test_dark_palette_tokens_present_in_css``
in :mod:`tests.integration.test_web_ui` — fetch the bundle via the
TestClient, substring-assert against rule fragments. No Playwright
dependency.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    """Local fixture so a CSS-only test doesn't depend on the
    test_web_ui seed/session fixtures."""
    saved = os.environ.get("KILN_DATABASE_URL")
    os.environ["KILN_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        if saved is None:
            os.environ.pop("KILN_DATABASE_URL", None)
        else:
            os.environ["KILN_DATABASE_URL"] = saved
        get_settings.cache_clear()


@pytest.fixture
def kiln_css(client: TestClient) -> str:
    response = client.get("/static/kiln.css")
    assert response.status_code == 200
    return response.text


def test_intermediate_breakpoint_641_960_present(kiln_css: str) -> None:
    """#289: intermediate tier between full desktop and the 640px
    mobile pass. Without it a tablet-portrait viewport (iPad 768,
    Surface 720) drops directly from 1.35rem desktop title to
    1.15rem mobile title — a typographic jump the audit flagged."""
    assert "@media (max-width: 960px) and (min-width: 641px)" in kiln_css


def test_intermediate_breakpoint_gutter_lands_between_tiers(
    kiln_css: str,
) -> None:
    """The intermediate gutter MUST sit between the desktop value
    (2.75rem) and the 640px-tier value (1.75rem). 2.2rem ≈ 35px is
    the deliberate middle ground."""
    assert "grid-template-columns: 2.2rem 1fr" in kiln_css


def test_intermediate_breakpoint_title_lands_between_tiers(
    kiln_css: str,
) -> None:
    """The intermediate title size MUST sit between desktop 1.35rem
    and 640px-tier 1.15rem. 1.25rem preserves more of the editorial
    heft than the 640 tier while still ceding inline space."""
    assert "font-size: 1.25rem" in kiln_css


def test_intermediate_breakpoint_index_softens_not_collapses(
    kiln_css: str,
) -> None:
    """The numbered gutter ('01', '02') stays display-grade-ish at
    this tier (opsz 72 between desktop's 144 and 640's 36). A jump
    straight to opsz 36 would lose the chapter-mark character before
    the inline width truly requires it."""
    assert 'font-variation-settings: "opsz" 72' in kiln_css


def test_mobile_breakpoint_640_present(kiln_css: str) -> None:
    """Primary mobile pass — covers iPhone SE, large phones, small
    tablets. Without this query the card grid stays at the desktop
    44px gutter on every viewport."""
    assert "@media (max-width: 640px)" in kiln_css


def test_mobile_breakpoint_360_present(kiln_css: str) -> None:
    """Worst-case (iPhone Mini, 320 Androids). Separate block from
    the 640 pass so the 375-480 tier — which the 640 rules already
    cover well — doesn't pay the extra tightening cost."""
    assert "@media (max-width: 360px)" in kiln_css


def test_mobile_card_shrinks_gutter(kiln_css: str) -> None:
    """The 44px desktop gutter is the legibility offender at 320px.
    Both passes shrink it; the 360 pass shrinks it further."""
    # 640px pass: gutter goes 2.75rem → 1.75rem
    assert "grid-template-columns: 1.75rem 1fr" in kiln_css
    # 360px pass: gutter goes further to 1.4rem
    assert "grid-template-columns: 1.4rem 1fr" in kiln_css


def test_mobile_index_softens_not_dropped(kiln_css: str) -> None:
    """The numbered gutter ('01', '02') is a signature element — it
    must SOFTEN on mobile, not disappear. Display-grade opsz drops to
    text-grade because the curves only earn their keep above ~1.4rem."""
    assert "font-size: 1.15rem" in kiln_css  # mid-tier
    assert "font-size: 1rem" in kiln_css  # worst-case
    # Display-grade opsz 144 (desktop) is replaced with text-grade.
    assert 'font-variation-settings: "opsz" 36' in kiln_css


def test_deprecation_stamp_owns_its_row_on_mobile(kiln_css: str) -> None:
    """Five-channel deprecation signal (PR #271) must read STRONGER,
    not weaker, when the header wraps on a narrow screen. The stamp
    breaks to its own row via flex-basis:100% so its verbal copy
    ('do not cite') is never crowded by a wrapping title."""
    assert "flex-basis: 100%" in kiln_css


def test_footer_stacks_with_score_first_on_mobile(kiln_css: str) -> None:
    """Score widget anchors the top of the stacked footer so the
    200ms-per-card scan still has its right-rail anchor. Without
    order:-1, the score would land BELOW the source-line and the
    scan loses its visual rhythm."""
    assert "flex-direction: column" in kiln_css
    assert "order: -1" in kiln_css


def test_excerpt_stays_above_wcag_body_floor(kiln_css: str) -> None:
    """Body text MUST NOT drop below 14px (~0.875rem) on any
    breakpoint per WCAG 1.4.4. The excerpt rule never gets resized
    in any of the new @media blocks; this guards against a future
    'just shrink it' attempt."""
    # No `.excerpt { font-size: <smaller> }` rule anywhere in the
    # bundle that would drop body text below 14px.
    matches = re.findall(
        r"\.excerpt\s*\{[^}]*?font-size:\s*([0-9.]+)rem",
        kiln_css,
        re.DOTALL,
    )
    sizes = [float(m) for m in matches]
    assert sizes, "expected at least one .excerpt font-size declaration"
    assert all(size >= 0.875 for size in sizes), (
        f"excerpt font-size dropped below 14px WCAG floor: {sizes!r}"
    )


def test_heading_path_hidden_only_at_worst_case(kiln_css: str) -> None:
    """The same {repo}/{path} renders in the footer source-line, so
    dropping the breadcrumb at 320px is a deliberate de-duplication —
    not a content loss. Pin the breakpoint so a future re-flow can't
    silently hide it at a wider width.

    Brace-matched extraction (no fixed-size window) — the test stays
    correct if future additions grow the @media body. Each ``{``
    nests one level; the block ends when the depth returns to zero.
    """

    def _block_body(at: str) -> str:
        start = kiln_css.index(at)
        # Skip to the opening brace of the @media block itself.
        open_idx = kiln_css.index("{", start)
        depth = 1
        i = open_idx + 1
        while i < len(kiln_css) and depth > 0:
            ch = kiln_css[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        return kiln_css[open_idx + 1 : i - 1]

    block_360 = _block_body("@media (max-width: 360px)")
    # The 360px block hides the breadcrumb.
    assert ".heading-path" in block_360
    assert "display: none" in block_360

    block_640 = _block_body("@media (max-width: 640px)")
    # The 640 block must NOT hide .heading-path (drops a signal users
    # still want at tablet sizes). Match the property:value pair to
    # tolerate whitespace differences inside the rule body.
    assert not re.search(r"\.heading-path\s*\{[^}]*display:\s*none", block_640, re.DOTALL)
