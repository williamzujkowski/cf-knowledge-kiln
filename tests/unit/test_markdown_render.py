"""#405 — sanitized markdown rendering for the preview panel.

Three constraint families under test:

1. **XSS**: raw HTML in markdown source is escaped (mistune ``escape=True``)
   AND nh3 strips ``script`` / ``iframe`` / ``style`` / ``on*`` / unsafe
   URL schemes as a second defense layer.

2. **CSP** (``style-src 'self'``, no ``unsafe-inline``): no inline
   ``style=`` attribute appears anywhere in the rendered output;
   Pygments runs in class mode and the token colors live in
   ``static/kiln/_code.css``.

3. **Heading order** (AAA 2.4.10): every markdown heading is demoted
   so the preview never emits ``<h1>`` or ``<h2>`` that would conflict
   with the panel's own ``<h2>`` title.

Plus the structural contract tests: paragraphs, lists, blockquotes,
tables, fenced code blocks all render correctly.
"""

from __future__ import annotations

import re
from pathlib import Path

from markupsafe import Markup

from cf_knowledge_kiln.api.markdown_render import render_markdown_safe

_REPO = Path(__file__).resolve().parents[2]
_CODE_CSS = _REPO / "src" / "cf_knowledge_kiln" / "api" / "static" / "kiln" / "_code.css"
_MAKEFILE = _REPO / "Makefile"


class TestReturnsMarkup:
    def test_returns_markupsafe(self) -> None:
        out = render_markdown_safe("hello")
        assert isinstance(out, Markup)

    def test_empty_returns_empty_markup(self) -> None:
        assert render_markdown_safe("") == Markup("")
        assert render_markdown_safe("   \n\n") == Markup("")


class TestStructuralRendering:
    """Basic markdown constructs render to expected HTML shapes."""

    def test_paragraph(self) -> None:
        out = str(render_markdown_safe("a paragraph here"))
        assert "<p>a paragraph here</p>" in out

    def test_unordered_list(self) -> None:
        out = str(render_markdown_safe("- one\n- two"))
        assert "<ul>" in out and "<li>one</li>" in out

    def test_ordered_list(self) -> None:
        out = str(render_markdown_safe("1. first\n2. second"))
        assert "<ol>" in out and "<li>first</li>" in out

    def test_blockquote(self) -> None:
        out = str(render_markdown_safe("> a quote"))
        assert "<blockquote>" in out and "a quote" in out

    def test_inline_code(self) -> None:
        out = str(render_markdown_safe("use `KILN_DATABASE_URL`"))
        assert "<code>KILN_DATABASE_URL</code>" in out

    def test_strong_em(self) -> None:
        out = str(render_markdown_safe("**bold** and *italic*"))
        assert "<strong>bold</strong>" in out
        assert "<em>italic</em>" in out

    def test_table_plugin_active(self) -> None:
        out = str(render_markdown_safe("| a | b |\n|---|---|\n| 1 | 2 |"))
        assert "<table>" in out and "<th>a</th>" in out and "<td>1</td>" in out

    def test_strikethrough_plugin(self) -> None:
        out = str(render_markdown_safe("~~struck~~"))
        assert "<del>struck</del>" in out


class TestHeadingDemotion:
    """AAA 2.4.10 / axe heading-order: the preview panel's own title
    is ``<h2>``; rendered markdown headings must NOT inject ``<h1>``
    or ``<h2>``. Every heading demotes by 2 levels, clamped at 6."""

    def test_h1_renders_as_h3(self) -> None:
        out = str(render_markdown_safe("# Title"))
        assert "<h3>Title</h3>" in out
        # Critical: NO h1 or h2 anywhere in the output.
        assert "<h1>" not in out
        assert "<h2>" not in out

    def test_h2_renders_as_h4(self) -> None:
        out = str(render_markdown_safe("## Sub"))
        assert "<h4>Sub</h4>" in out
        assert "<h2>" not in out

    def test_h3_renders_as_h5(self) -> None:
        out = str(render_markdown_safe("### Section"))
        assert "<h5>Section</h5>" in out

    def test_h4_renders_as_h6(self) -> None:
        out = str(render_markdown_safe("#### Deep"))
        assert "<h6>Deep</h6>" in out

    def test_h5_clamped_at_h6(self) -> None:
        out = str(render_markdown_safe("##### Deeper"))
        assert "<h6>Deeper</h6>" in out

    def test_h6_clamped_at_h6(self) -> None:
        out = str(render_markdown_safe("###### Deepest"))
        assert "<h6>Deepest</h6>" in out


class TestFencedCodeBlocks:
    """Pygments class-mode highlighting. No inline styles (CSP).
    Wrapper class ``codehilite`` is what ``_code.css`` targets."""

    def test_fenced_python_emits_codehilite_wrapper(self) -> None:
        out = str(render_markdown_safe('```python\nprint("hi")\n```'))
        assert 'class="codehilite"' in out

    def test_fenced_python_has_token_spans(self) -> None:
        """Pygments produces ``<span class="k">…</span>`` etc. for
        token classes. Verifying the keyword span proves the
        class-mode highlighter ran and the CSS will color it."""
        out = str(render_markdown_safe("```python\nimport os\nfor x in []: pass\n```"))
        # ``import`` is a keyword.namespace token (.kn or .k).
        assert re.search(r'<span class="(k|kn)">import</span>', out), (
            f"expected a keyword span around 'import'; got {out[:500]}"
        )

    def test_fenced_yaml_emits_token_spans(self) -> None:
        out = str(render_markdown_safe("```yaml\nkey: value\n```"))
        assert 'class="codehilite"' in out
        # YAML keys are name.tag (.nt) tokens.
        assert "<span" in out

    def test_unknown_language_falls_back_to_plain(self) -> None:
        """A fence with an unknown language renders without
        highlighting but still produces the codehilite wrapper +
        escaped content. Doesn't raise."""
        out = str(render_markdown_safe("```not-a-real-lang\nfoo bar\n```"))
        assert 'class="codehilite"' in out
        assert "foo bar" in out

    def test_no_language_falls_back_to_plain(self) -> None:
        out = str(render_markdown_safe("```\nplain text\n```"))
        assert 'class="codehilite"' in out
        assert "plain text" in out

    def test_html_inside_code_block_escaped(self) -> None:
        """Code blocks MUST NOT interpret HTML in the fence body.
        ``<script>`` in a YAML/HTML/bash example renders as text —
        Pygments tokenizes the angle brackets + name into separate
        spans so the literal substring ``&lt;script&gt;`` isn't
        contiguous, but the unencoded ``<script>alert(1)</script>``
        must never appear in the output."""
        out = str(render_markdown_safe("```html\n<script>alert(1)</script>\n```"))
        # No real <script> element opens.
        assert "<script>alert(1)" not in out
        # Angle brackets are escaped somewhere in the output.
        assert "&lt;" in out and "&gt;" in out


class TestCspNoInlineStyles:
    """Critical constraint: the deployed CSP ships
    ``style-src 'self'`` without ``unsafe-inline``. ANY ``style=``
    attribute in the rendered output gets BLOCKED by the browser.
    Pygments class mode + nh3's ``style`` strip both enforce this."""

    def test_no_inline_style_attribute_anywhere(self) -> None:
        """Hammer the renderer with every construct that could
        plausibly emit ``style=`` and assert nothing leaks."""
        md = """\
# Heading 1
## Heading 2
A paragraph with **bold** and *italic*.

- list item
- item with `code`

> blockquote

| col | col |
|---|---|
| a | b |

```python
def f():
    return 42
```

```yaml
key: value
nested:
  - one
  - two
```
"""
        out = str(render_markdown_safe(md))
        # 'style=' anywhere is a CSP violation.
        assert "style=" not in out, f"inline style= leaked: {out[:1000]}"

    def test_raw_style_attribute_in_source_stripped(self) -> None:
        """Even if the markdown source CONTAINS literal HTML with
        ``style=``, mistune escapes it (``escape=True``) and nh3
        would also strip the attribute. The key test is: no real
        HTML element comes out with a ``style`` attribute. The
        escaped TEXT may legitimately contain the substring
        ``style="..."`` because the whole raw HTML is now visible
        text content."""
        md = '<p style="color:red">hi</p>'
        out = str(render_markdown_safe(md))
        # No real <p style="..."> element is emitted (the source
        # <p> was escaped, then the renderer wraps escaped text in
        # its own clean <p>...</p>).
        assert re.search(r"<\w+[^>]*\sstyle\s*=", out) is None, (
            f"a real element has a style= attribute: {out!r}"
        )


class TestXssGuards:
    """nh3 + mistune ``escape=True`` form the trust boundary. Pin
    every common XSS shape so a future renderer regression is
    caught immediately."""

    def test_script_tag_escaped(self) -> None:
        out = str(render_markdown_safe("<script>alert(1)</script>"))
        assert "<script>" not in out

    def test_iframe_stripped(self) -> None:
        out = str(render_markdown_safe('<iframe src="evil"></iframe>'))
        assert "<iframe" not in out

    def test_img_onerror_stripped(self) -> None:
        """``<img src=x onerror=alert(1)>`` is the textbook XSS
        payload. The actual image element + the event handler
        attribute must NOT appear as a real element/attribute. The
        escaped TEXT content of the source string is fine — it's
        just visible characters now, can't execute."""
        out = str(render_markdown_safe('<img src=x onerror="alert(1)">'))
        # No real <img> element opens.
        assert "<img " not in out and "<img>" not in out
        # No real on* attribute on any element.
        assert re.search(r"<\w+[^>]*\son\w+\s*=", out) is None, (
            f"event-handler attribute leaked: {out!r}"
        )

    def test_javascript_href_neutered(self) -> None:
        out = str(render_markdown_safe("[click](javascript:alert(1))"))
        # The anchor text survives so the user sees the link wasn't
        # silently dropped; but ``javascript:`` URL is replaced.
        assert "click" in out
        assert "javascript:" not in out

    def test_data_href_neutered(self) -> None:
        out = str(render_markdown_safe("[x](data:text/html,<script>1</script>)"))
        assert "data:text/html" not in out

    def test_form_input_stripped(self) -> None:
        out = str(render_markdown_safe('<form action="evil"><input name="x"></form>'))
        assert "<form" not in out
        assert "<input" not in out

    def test_object_embed_stripped(self) -> None:
        out = str(render_markdown_safe('<object data="evil.swf"></object>'))
        assert "<object" not in out

    def test_link_meta_stripped(self) -> None:
        """<link> and <meta> can pull in remote resources or alter
        the document head. Strip both regardless of attrs."""
        out = str(
            render_markdown_safe(
                '<link rel="stylesheet" href="evil.css">'
                '<meta http-equiv="refresh" content="0;url=evil">'
            )
        )
        assert "<link" not in out
        assert "<meta" not in out

    def test_external_link_forces_rel_noopener(self) -> None:
        """nh3 ``link_rel`` config forces ``rel="noopener noreferrer"``
        on every anchor. Same-tab opening risk + tab-nabbing both
        mitigated."""
        out = str(render_markdown_safe("[home](https://example.com)"))
        assert 'href="https://example.com"' in out
        assert "noopener" in out
        assert "noreferrer" in out


class TestPygmentsTokenCssHelper:
    """The :func:`pygments_token_css` helper exists for rebuild
    workflows (Pygments upgrade → regenerate the vendored CSS).
    Pin it returns the prefixed token rules."""

    def test_returns_string(self) -> None:
        from cf_knowledge_kiln.api.markdown_render import pygments_token_css

        css = pygments_token_css()
        assert isinstance(css, str)
        assert ".codehilite" in css

    def test_custom_prefix(self) -> None:
        from cf_knowledge_kiln.api.markdown_render import pygments_token_css

        css = pygments_token_css(prefix=".kiln-code")
        assert ".kiln-code" in css


class TestCodeCssPartial:
    """The vendored token sheet ships AAA-safe colors + the
    forced-colors fallback + the scroll-region focus ring."""

    def test_partial_exists(self) -> None:
        assert _CODE_CSS.exists()

    def test_styles_codehilite_wrapper(self) -> None:
        css = _CODE_CSS.read_text(encoding="utf-8")
        assert ".codehilite" in css

    def _css_without_comments(self) -> str:
        """Strip /* ... */ comment blocks so substring checks for
        hex values don't false-positive on rationale comments that
        cite the Pygments defaults explicitly."""
        css = _CODE_CSS.read_text(encoding="utf-8")
        return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    def test_keyword_uses_aaa_palette(self) -> None:
        """Pygments default keyword color (#007020) is 6.1:1 on
        --paper, not AAA. We override with --ink (9.5:1).
        Comments may cite the default hex; only non-comment rules
        must not reference it."""
        rules = self._css_without_comments()
        assert "#007020" not in rules, (
            "Pygments default keyword color leaked into the kiln "
            "stylesheet rules — should be using the AAA palette tokens."
        )

    def test_string_uses_aaa_palette(self) -> None:
        """Pygments default string color (#4070A0) is 4.5:1. Ditto."""
        rules = self._css_without_comments()
        assert "#4070A0" not in rules

    def test_focus_ring_on_scrollable_region(self) -> None:
        """AAA B2: scrollable <pre> must have keyboard-visible focus."""
        css = _CODE_CSS.read_text(encoding="utf-8")
        assert ".codehilite:focus-within" in css

    def test_forced_colors_block(self) -> None:
        css = _CODE_CSS.read_text(encoding="utf-8")
        assert "@media (forced-colors: active)" in css

    def test_prefers_contrast_block(self) -> None:
        css = _CODE_CSS.read_text(encoding="utf-8")
        assert "@media (prefers-contrast: more)" in css


class TestBundleOrder:
    """``_code.css`` must concatenate so the .codehilite rules
    appear in the bundled kiln.css. Pin its presence."""

    def test_code_partial_bundled(self) -> None:
        mk = _MAKEFILE.read_text(encoding="utf-8")
        assert "_code.css" in mk


class TestPreviewTemplateContract:
    """The template change is the user-visible piece. Pin the new
    markup shape so a future template refactor doesn't silently
    revert to the raw ``<pre>`` rendering."""

    def test_template_renders_kiln_prose_div_for_html(self) -> None:
        src = (
            _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates" / "_preview.html"
        ).read_text(encoding="utf-8")
        assert 'class="preview-body kiln-prose markdown"' in src
        assert "target.content_html" in src

    def test_template_falls_back_to_pre_when_html_empty(self) -> None:
        """If ``content_html`` is empty (whitespace-only chunk),
        the template renders the legacy <pre> so the panel never
        shows a blank box."""
        src = (
            _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates" / "_preview.html"
        ).read_text(encoding="utf-8")
        # The {% else %} branch renders <pre class="preview-body">.
        assert "{% else %}" in src

    def test_target_region_is_focusable_and_labeled(self) -> None:
        """AAA B2 — scrollable region needs ``tabindex=0`` + a label."""
        src = (
            _REPO / "src" / "cf_knowledge_kiln" / "api" / "templates" / "_preview.html"
        ).read_text(encoding="utf-8")
        assert 'tabindex="0"' in src
        assert 'role="region"' in src
        assert 'aria-label="Selected chunk content"' in src


class TestPreviewRouteThreadsContentHtml:
    """The route must populate ``target.content_html`` so the
    template's preferred branch fires. Pin the wiring."""

    def test_preview_module_imports_renderer(self) -> None:
        src = (_REPO / "src" / "cf_knowledge_kiln" / "api" / "preview.py").read_text(
            encoding="utf-8"
        )
        assert "from cf_knowledge_kiln.api.markdown_render import render_markdown_safe" in src

    def test_preview_module_threads_content_html(self) -> None:
        src = (_REPO / "src" / "cf_knowledge_kiln" / "api" / "preview.py").read_text(
            encoding="utf-8"
        )
        assert '"content_html": render_markdown_safe(target.content)' in src
