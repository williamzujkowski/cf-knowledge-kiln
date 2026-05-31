"""Render surfaced corpus markdown to sanitized, CSP-safe HTML (#405).

The kiln UI surfaces indexed-document content in the preview panel.
That content is markdown checked out from repos — *evidence*, not
trusted input (the masthead notice and the agent API both frame it
that way). So this module renders markdown → HTML through three
hard constraints:

1. **XSS.** Raw inline HTML in the source is escaped, never passed
   through (``mistune.create_markdown(escape=True)``), and the
   rendered output is then run through :func:`nh3.clean` with a
   tight tag/attr allowlist. ``style=`` and every ``on*`` handler
   are dropped; link schemes are capped at http/https/mailto; every
   external ``<a>`` has ``rel="noopener noreferrer" target="_blank"``
   forced on.

2. **CSP ``style-src 'self'``.** The deployed CSP ships no
   ``unsafe-inline`` (see ``api/csp.py``). Pygments' default
   formatter emits inline ``style="color:#…"`` spans, which the
   browser blocks under that policy. So code is highlighted in
   **class mode** (``noclasses=False``) — token ``<span class="k">``
   etc. — and the colors live in a vendored stylesheet
   ``static/kiln/_code.css`` served from ``'self'``. No inline
   styles are ever emitted here.

3. **Heading order / AAA 2.4.10.** The preview panel's own title is
   an ``<h2>``. Markdown ``#``/``##`` would inject ``<h1>``/``<h2>``
   and break document heading order, so every rendered heading is
   demoted by 2 levels (``# Title`` → ``<h3>``, ``## Sub`` → ``<h4>``,
   clamped at ``<h6>``).

The result is wrapped in :class:`markupsafe.Markup` so the Jinja
autoescape in the template leaves it intact (same pattern as
``result_cards.highlight_excerpt``).
"""

from __future__ import annotations

import logging

import mistune
import nh3
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

logger = logging.getLogger(__name__)

# Pygments class-mode formatter. ``noclasses=False`` means token
# colors are emitted as ``<span class="k">`` etc. instead of inline
# ``style=`` attributes (the CSP-killer). ``cssclass`` is the wrapper
# class the vendored ``_code.css`` selector targets. ``wrapcode``
# wraps the highlighted body in ``<code>`` so the markup matches the
# ``<pre><code class="language-X">`` shape downstream tooling expects.
_PYGMENTS_FORMATTER = HtmlFormatter(
    noclasses=False,
    cssclass="codehilite",
    wrapcode=True,
)


class _KilnRenderer(mistune.HTMLRenderer):  # type: ignore[misc, unused-ignore]
    r"""Mistune renderer with Pygments class-mode codeblocks + heading demotion.

    Inherits the default ``HTMLRenderer`` behavior for paragraphs,
    lists, blockquotes, emphasis, etc. Only ``block_code`` and
    ``heading`` are overridden:

    * **block_code** routes through Pygments in class mode. The
      ``info`` argument is the fence language (``\`\`\`python``);
      missing / unknown language falls back to :class:`TextLexer`
      so the block still renders, just without highlighting.

    * **heading** demotes the level by :attr:`_HEADING_OFFSET` so
      the rendered HTML never produces ``<h1>``/``<h2>`` that would
      break the document outline. ``<h3>`` is the lowest level the
      preview panel can take in (the panel itself uses ``<h2>``).
    """

    _HEADING_OFFSET = 2

    def heading(self, text: str, level: int, **attrs: object) -> str:  # noqa: ARG002 — attrs from mistune
        demoted = min(level + self._HEADING_OFFSET, 6)
        return f"<h{demoted}>{text}</h{demoted}>\n"

    def block_code(self, code: str, info: str | None = None) -> str:
        lang = (info or "").strip().split(None, 1)[0] if info else ""
        try:
            lexer = get_lexer_by_name(lang) if lang else TextLexer()
        except ClassNotFound:
            lexer = TextLexer()
        # ``highlight`` returns a complete <div class="codehilite"><pre>…</pre></div>.
        # Pygments class mode emits no inline styles.
        out: str = highlight(code, lexer, _PYGMENTS_FORMATTER)
        return out


# Single shared markdown instance — mistune compiles the AST once.
# ``escape=True`` is the load-bearing safety switch: raw HTML blocks
# in the markdown source get escaped rather than passed through.
# Plugins (``table``, ``strikethrough``, ``url``) render via the
# default safe paths.
_MARKDOWN = mistune.create_markdown(
    renderer=_KilnRenderer(escape=True),
    plugins=["table", "strikethrough", "url"],
    hard_wrap=False,
)


# ── nh3 sanitizer config ─────────────────────────────────────────────
#
# The renderer above is the FIRST trust boundary. nh3 is the SECOND:
# even if a renderer bug or a plugin emitted unexpected tags/attrs,
# this allowlist would still strip them before the HTML hits the
# template. Defense in depth.
#
# Tag list: everything mistune emits for the constructs we care about,
# plus the Pygments token wrapper (``span``, ``pre``, ``code``).
# Excluded explicitly: ``script``, ``iframe``, ``object``, ``embed``,
# ``form``, ``input``, ``style``, ``link``, ``meta`` (the dangerous
# set; nh3's default also covers most of these but we list them by
# absence here for clarity).

_ALLOWED_TAGS: set[str] = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "div",  # Pygments codehilite wrapper
    "em",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "span",  # Pygments token spans
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}

# ``class`` is allowed ONLY on the Pygments output (codehilite wrapper
# + token spans) so a malicious markdown can't smuggle a class that
# triggers an unintended CSS rule. nh3 expects {tag: {attr, ...}}.
_ALLOWED_ATTRS: dict[str, set[str]] = {
    # ``rel`` is intentionally NOT in the <a> attr set — nh3's
    # ``link_rel`` parameter forces it on every external link, and
    # listing it here too would panic (nh3 4.x asserts no overlap).
    # ``target`` is allowed so external links open in a new tab.
    "a": {"href", "title", "target"},
    "code": {"class"},
    "div": {"class"},  # for <div class="codehilite">
    "pre": {"class"},
    "span": {"class"},
    "th": {"align"},
    "td": {"align"},
}

# URL schemes for <a href=...>. mistune already neuters most of the
# bad ones at the renderer layer, but nh3 enforces a second pass.
_URL_SCHEMES: set[str] = {"http", "https", "mailto"}


def render_markdown_safe(text: str) -> Markup:
    """Render markdown → CSP-safe, XSS-safe HTML wrapped in :class:`Markup`.

    Empty / whitespace-only input returns ``Markup("")`` so the
    template can use ``{{ x.content_html }}`` unconditionally.

    The returned value is XSS-safe by construction (see module
    docstring). Pass to Jinja templates without ``|safe``; the
    ``Markup`` wrapper signals to autoescape that this string is
    already-escaped HTML.
    """
    if not text or not text.strip():
        return Markup("")
    # mistune's create_markdown(renderer=...) can also be configured
    # for token-stream output (returns list[dict]), but with our
    # HTMLRenderer subclass the call site always gets str. Cast to
    # narrow the mypy union.
    rendered = str(_MARKDOWN(text))
    cleaned: str = nh3.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
    # The renderer + nh3 are the trust boundary; wrap so Jinja
    # autoescape lets the result through verbatim. nh3.clean() above
    # ran tag/attr/url allowlists; mistune ran with escape=True. Both
    # tools are designed exactly for this "render untrusted markdown
    # to safe HTML" use case. The bandit B704 flag is the right
    # general-purpose warning but doesn't apply here.
    return Markup(cleaned)  # noqa: S704 # nosec B704 — safety enforced upstream


def pygments_token_css(*, prefix: str = ".codehilite") -> str:
    """Return the CSS token color rules for the Pygments class mode.

    Vendored at build time into ``static/kiln/_code.css`` so the page
    works under ``style-src 'self'`` (no inline ``style=``). Default
    style is ``friendly`` — readable on light backgrounds; the
    consuming partial overrides individual colors against the kiln
    palette tokens to land AAA contrast for the most-common token
    classes.

    Exposed as a helper so a future rebuild pass (after a Pygments
    upgrade) can regenerate via a one-liner script without
    re-deriving the prefix.
    """
    css: str = HtmlFormatter(style="friendly").get_style_defs(prefix)
    return css


__all__ = ["pygments_token_css", "render_markdown_safe"]
