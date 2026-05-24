"""Tests for Markdown chunking + frontmatter parsing (#15)."""

from __future__ import annotations

import textwrap

import pytest

from cf_knowledge_kiln.ingestion.chunking import (
    Chunk,
    parse_document,
)
from cf_knowledge_kiln.ingestion.tokens import count_tokens


def test_empty_document_yields_no_chunks() -> None:
    doc = parse_document("")
    assert doc.chunks == []
    assert doc.meta == {}


def test_frontmatter_extracted() -> None:
    src = textwrap.dedent(
        """\
        ---
        title: Hello
        owner: platform
        status: active
        ---
        # Hello
        Body.
        """
    )
    doc = parse_document(src)
    assert doc.meta["title"] == "Hello"
    assert doc.meta["owner"] == "platform"
    assert doc.meta["status"] == "active"
    assert doc.title == "Hello"


def test_frontmatter_with_yaml_date_normalizes_to_iso_string() -> None:
    """#91: a YAML ``date:`` field used to crash the JSONB upsert.

    The parser now coerces it to ISO-8601 before handing off, so the
    meta dict round-trips through ``json.dumps`` cleanly.
    """
    import json

    src = textwrap.dedent(
        """\
        ---
        id: ADR-0001
        date: 2026-05-16
        ---
        # ADR
        Body.
        """
    )
    doc = parse_document(src)
    assert doc.meta["date"] == "2026-05-16"
    # Round-trip: this is what asyncpg+JSONB needs.
    json.dumps(doc.meta)


def test_title_falls_back_to_first_h1_when_frontmatter_absent() -> None:
    doc = parse_document("# Top\nBody.\n")
    assert doc.title == "Top"


def test_simple_document_one_section_one_chunk() -> None:
    doc = parse_document("# Top\nBody.\n")
    assert len(doc.chunks) == 1
    chunk = doc.chunks[0]
    assert chunk.chunk_index == 0
    assert chunk.heading_path == ["Top"]
    assert chunk.content_tokens > 0
    assert chunk.content_hash.startswith("sha256:")


def test_heading_path_tracks_nesting() -> None:
    src = textwrap.dedent(
        """\
        # Top
        intro
        ## A
        a body
        ### A.1
        a.1 body
        ## B
        b body
        """
    )
    doc = parse_document(src)
    paths = [c.heading_path for c in doc.chunks]
    assert paths == [
        ["Top"],
        ["Top", "A"],
        ["Top", "A", "A.1"],
        ["Top", "B"],
    ]


def test_h3_resets_path_when_h2_resumes() -> None:
    src = textwrap.dedent(
        """\
        # Top
        ## Section
        ### Sub
        sub body
        ## Other
        other body
        """
    )
    doc = parse_document(src)
    last_path = doc.chunks[-1].heading_path
    assert last_path == ["Top", "Other"]


def test_code_block_is_preserved_in_a_single_chunk() -> None:
    src = textwrap.dedent(
        """\
        # Top
        Before.

        ```python
        def foo():
            return 1
        ```

        After.
        """
    )
    doc = parse_document(src)
    # All code-fence content stays in one chunk.
    full = "\n".join(c.content for c in doc.chunks)
    assert "def foo():" in full
    assert "```python" in full and full.count("```") == 2


def test_table_block_is_preserved_in_a_single_chunk() -> None:
    src = textwrap.dedent(
        """\
        # Top
        | a | b |
        | --- | --- |
        | 1 | 2 |
        | 3 | 4 |

        After.
        """
    )
    doc = parse_document(src)
    full = "\n".join(c.content for c in doc.chunks)
    assert "| a | b |" in full
    assert "| 3 | 4 |" in full


def test_list_block_is_preserved_in_a_single_chunk() -> None:
    src = textwrap.dedent(
        """\
        # Top
        - first
        - second
          - nested
        - third

        After.
        """
    )
    doc = parse_document(src)
    full = "\n".join(c.content for c in doc.chunks)
    assert "- first" in full
    assert "  - nested" in full


def test_large_section_is_split_under_max_tokens() -> None:
    # Build a section much larger than max_tokens=200.
    para = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 30
    src = "# Top\n\n" + ("\n\n".join([para] * 6))
    doc = parse_document(src, max_tokens=200)
    assert len(doc.chunks) >= 2
    for chunk in doc.chunks:
        assert chunk.content_tokens <= 200 or _is_single_atomic_block(chunk)


def _is_single_atomic_block(chunk: Chunk) -> bool:
    """A chunk over-cap is OK only if it's one atomic block (rare)."""
    body = chunk.content.strip()
    return "\n\n" not in body


def test_token_counts_match_tiktoken() -> None:
    doc = parse_document("# Top\nHello world.\n")
    chunk = doc.chunks[0]
    assert chunk.content_tokens == count_tokens(chunk.content)


def test_content_hash_is_stable_across_runs() -> None:
    a = parse_document("# Top\nhello\n").chunks[0]
    b = parse_document("# Top\nhello\n").chunks[0]
    assert a.content_hash == b.content_hash


def test_content_hash_differs_on_different_content() -> None:
    a = parse_document("# Top\nhello\n").chunks[0]
    b = parse_document("# Top\ngoodbye\n").chunks[0]
    assert a.content_hash != b.content_hash


def test_deeply_nested_headings_keep_full_path() -> None:
    src = textwrap.dedent(
        """\
        # H1
        ## H2
        ### H3
        #### H4
        ##### H5
        ###### H6
        leaf body
        """
    )
    doc = parse_document(src)
    assert doc.chunks[-1].heading_path == ["H1", "H2", "H3", "H4", "H5", "H6"]


def test_chunk_indexes_are_sequential() -> None:
    src = textwrap.dedent(
        """\
        # A
        body
        # B
        body
        # C
        body
        """
    )
    doc = parse_document(src)
    assert [c.chunk_index for c in doc.chunks] == [0, 1, 2]


def test_nested_fenced_code_block_is_preserved_with_inner_fence() -> None:
    """A ```` ```` outer fence must NOT close on an inner ``` line."""
    src = textwrap.dedent(
        """\
        # Top
        ````md
        Outer.
        ```python
        def foo(): pass
        ```
        More outer.
        ````
        After.
        """
    )
    doc = parse_document(src)
    full = "\n".join(c.content for c in doc.chunks)
    # Both inner triple-backticks survive intact.
    assert full.count("```python") == 1
    assert full.count("def foo()") == 1
    # The outer fence open/close (4 backticks) is also present.
    assert "````md" in full
    # The "After." text lands in a *separate* chunk concept (after the
    # outer fence closes), or stays in the same section — either way it
    # must appear unmangled.
    assert "After." in full


def test_frontmatter_overrides_apply_per_document() -> None:
    src = textwrap.dedent(
        """\
        ---
        owner: cybersecurity
        status: deprecated
        ---
        # Body
        text
        """
    )
    doc = parse_document(src)
    assert doc.meta["owner"] == "cybersecurity"
    assert doc.meta["status"] == "deprecated"


# ─── frontmatter size cap (#54) ─────────────────────────────────────


def test_oversize_frontmatter_raises_dedicated_error() -> None:
    """#54: frontmatter past MAX_FRONTMATTER_BYTES rejects with a clean error.

    A multi-megabyte YAML blob in frontmatter would otherwise become a
    JSONB row that's expensive to write, slow to query, and (worst case)
    an OOM vector. Cap defensively at the parser.
    """
    from cf_knowledge_kiln.ingestion.chunking import (
        MAX_FRONTMATTER_BYTES,
        FrontmatterTooLargeError,
        parse_document,
    )

    huge = "x" * (MAX_FRONTMATTER_BYTES + 1)
    src = f"---\nbig: {huge}\n---\n# Body\nok\n"
    with pytest.raises(FrontmatterTooLargeError, match="frontmatter is"):
        parse_document(src)


def test_just_under_cap_frontmatter_parses_normally() -> None:
    """Adjacent boundary case — sized just under the cap parses cleanly."""
    from cf_knowledge_kiln.ingestion.chunking import (
        MAX_FRONTMATTER_BYTES,
        parse_document,
    )

    # Reserve a few bytes for the YAML scaffolding ("big: ").
    payload = "x" * (MAX_FRONTMATTER_BYTES - 100)
    src = f"---\nbig: {payload}\n---\n# Body\nok\n"
    doc = parse_document(src)
    assert doc.title == "Body"
    assert doc.meta["big"] == payload


def test_no_frontmatter_skips_size_check() -> None:
    """Docs with no frontmatter at all are unaffected by the cap."""
    from cf_knowledge_kiln.ingestion.chunking import parse_document

    doc = parse_document("# Top\nbody\n")
    assert doc.title == "Top"


# ─── source_url scheme allowlist (#24 reviewer HIGH) ────────────────


def test_safe_source_url_accepts_http_and_https() -> None:
    from cf_knowledge_kiln.ingestion._file_processing import _safe_source_url

    assert _safe_source_url("https://docs.example.com/x") == "https://docs.example.com/x"
    assert _safe_source_url("http://docs.example.com/x") == "http://docs.example.com/x"


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(document.cookie)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "mailto:attacker@example.com",
    ],
)
def test_safe_source_url_rejects_non_http_schemes(hostile: str) -> None:
    """#24 HIGH: stored-XSS prevention. Anything not http(s) → None."""
    from cf_knowledge_kiln.ingestion._file_processing import _safe_source_url

    assert _safe_source_url(hostile) is None


def test_safe_source_url_rejects_empty_and_non_string() -> None:
    from cf_knowledge_kiln.ingestion._file_processing import _safe_source_url

    assert _safe_source_url(None) is None
    assert _safe_source_url("") is None
    assert _safe_source_url("   ") is None
    assert _safe_source_url(42) is None
    assert _safe_source_url(["https://x"]) is None


def test_safe_source_url_rejects_scheme_without_netloc() -> None:
    """``https:`` with no host is not a valid absolute URL."""
    from cf_knowledge_kiln.ingestion._file_processing import _safe_source_url

    assert _safe_source_url("https:") is None
    # A relative path is not an absolute URL.
    assert _safe_source_url("/runbooks/foo") is None


# ─── #201 — heading-only sections are dropped ─────────────────────────


def test_h2_with_no_preamble_before_nested_h3_does_not_emit_stub() -> None:
    """#201: an H2 with no body that's followed by a nested H3 used to
    emit a 2-token "## Heading" chunk. The H2 is still in the H3's
    heading_path; the standalone stub was pure index pollution.
    """
    src = textwrap.dedent(
        """\
        # Top

        ## Configuration
        ### Database
        The connection settings live in here.
        """
    )
    doc = parse_document(src)
    # Pre-fix: 2-3 chunks including a "## Configuration" stub.
    # Post-fix: 1 chunk for the H3 body, no Configuration stub.
    assert len(doc.chunks) == 1
    chunk = doc.chunks[0]
    assert chunk.heading_path == ["Top", "Configuration", "Database"]
    assert "## Configuration" not in chunk.content or "### Database" in chunk.content
    # The path retains "Configuration" so no information is lost.
    assert "Configuration" in chunk.heading_path


def test_h1_with_no_preamble_before_first_h2_does_not_emit_stub() -> None:
    """#201, H1 variant: a doc that opens with ``# Title`` then jumps
    straight to ``## Section`` used to emit a standalone H1 chunk.
    """
    src = textwrap.dedent(
        """\
        # Caddy Reverse Proxy

        ## Overview
        Routes traffic from edge to backend services.
        """
    )
    doc = parse_document(src)
    assert len(doc.chunks) == 1
    chunk = doc.chunks[0]
    assert chunk.heading_path == ["Caddy Reverse Proxy", "Overview"]
    # The H1 title is in the path, not as a standalone heading-only chunk.
    assert chunk.content_tokens > 5  # Pre-fix the H1 chunk was 5 tokens.


def test_pure_heading_doc_yields_no_chunks() -> None:
    """#201 edge case: a doc that is ONLY headings (no body anywhere)
    produces zero chunks. Previously each heading became its own stub.

    This is intentional — a heading-only doc has no retrievable content
    once the heading itself is the only text. Folding it into a chunk
    would not help retrieval (no body to embed against). Ingestion
    callers can detect zero-chunk docs and report them as empty.
    """
    src = textwrap.dedent(
        """\
        # Top

        ## Section A
        ## Section B
        ### Subsection
        """
    )
    doc = parse_document(src)
    assert doc.chunks == []


def test_heading_only_section_followed_by_sibling_with_content_keeps_sibling() -> None:
    """Skipping heading-only sections must NOT eat real content sections.

    Validates the fix is surgical — only stripped-stub sections vanish.
    """
    src = textwrap.dedent(
        """\
        # Top

        ## Empty Section
        ## Real Section
        Real content lives here.
        """
    )
    doc = parse_document(src)
    assert len(doc.chunks) == 1
    chunk = doc.chunks[0]
    assert chunk.heading_path == ["Top", "Real Section"]
    assert "Real content" in chunk.content


# ─── #200 — nested fence handling ─────────────────────────────────────


def test_nested_same_backtick_fence_does_not_close_outer_early() -> None:
    """#200: a ``` markdown ``` template containing an inner ``` bash ```
    block must keep the OUTER block atomic.

    Pre-fix: the inner bash fence's closing ``` was treated as closing
    the outer markdown block. The chunker then walked past it as if the
    template body were top-level, emitting H2 lines like
    ``## Root-cause investigation`` as their own section boundaries —
    spraying the index with stub-shaped chunks and (in the worst case)
    swallowing the real ``## Index`` that came after the outer close.

    The repro shape comes from
    williamzujkowski/homelab-iac/docs/runbooks/README.md (lines 23-51).
    """
    src = textwrap.dedent(
        """\
        # Alert Runbooks

        ## How to add a new runbook

        1. Copy the template.
        2. Customize.

        ```markdown
        # <AlertName>

        ## What's happening

        Description.

        ## First checks (30 seconds)

        ```bash
        # First command
        ```

        ## Root-cause investigation

        Steps.

        ## Resolution

        Concrete fix.

        ## Related

        - [Component doc](../components/X.md)
        ```

        ## Index

        Sorted alphabetically.
        """
    )
    doc = parse_document(src)
    # The outer ```markdown``` fence is one atomic block, so the only
    # heading-bearing sections are "How to add a new runbook" and "Index".
    # ("Alert Runbooks" itself has no body before its first H2 → dropped
    # by #201; that's correct.)
    paths = [c.heading_path for c in doc.chunks]
    assert paths == [
        ["Alert Runbooks", "How to add a new runbook"],
        ["Alert Runbooks", "Index"],
    ]
    # The inner-template headings must NOT appear as chunk-path segments.
    flat = {seg for path in paths for seg in path}
    assert "Root-cause investigation" not in flat
    assert "Resolution" not in flat
    assert "Related" not in flat
    # The whole template, both fences and the inner bash block, lives in
    # the first chunk's content.
    first = doc.chunks[0].content
    assert "```markdown" in first
    assert "```bash" in first
    # The template close-fence and the post-template Index header live
    # in different chunks (= the chunker found the real outer close).
    assert "## Index" not in first
    assert "## Index" in doc.chunks[1].content


def test_simple_python_fence_still_handled_correctly() -> None:
    """No-regression: a plain ``` python ``` block still works."""
    src = textwrap.dedent(
        """\
        # Top
        Before.

        ```python
        def foo():
            return 1
        ```

        After.
        """
    )
    doc = parse_document(src)
    full = "\n".join(c.content for c in doc.chunks)
    assert "def foo():" in full
    assert full.count("```") == 2


def test_tilde_fence_inside_backtick_fence_treated_as_content() -> None:
    """A ``~~~`` line inside a ``` ``` `` block must NOT close it (different fence types)."""
    src = textwrap.dedent(
        """\
        # Top

        ```
        Some text
        ~~~
        Even more text inside
        ~~~
        Final text
        ```

        After.
        """
    )
    doc = parse_document(src)
    # Everything between the outer ``` fences should be one block.
    body_with_fence = "".join(c.content for c in doc.chunks)
    assert "Some text" in body_with_fence
    assert "Final text" in body_with_fence
    assert "After." in body_with_fence
    # No spurious header chunks injected mid-fence.
    paths = [c.heading_path for c in doc.chunks]
    assert all(p == ["Top"] for p in paths)


def test_four_backtick_outer_with_three_backtick_inner_works() -> None:
    """Conformant CommonMark nesting: ````outer```` contains ```inner```."""
    src = textwrap.dedent(
        """\
        # Top

        ````markdown
        Use ```python``` for inline code.

        ```python
        def foo(): pass
        ```

        Then continue.
        ````

        After.
        """
    )
    doc = parse_document(src)
    full = "".join(c.content for c in doc.chunks)
    # The whole 4-backtick outer fence is one block, and "After." came after it.
    assert "def foo(): pass" in full
    assert "After." in full
    # No internal "## X" got promoted (there aren't any here, but assertion is cheap).
    paths = [c.heading_path for c in doc.chunks]
    assert all(p == ["Top"] for p in paths)
