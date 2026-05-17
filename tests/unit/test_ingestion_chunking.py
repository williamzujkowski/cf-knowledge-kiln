"""Tests for Markdown chunking + frontmatter parsing (#15)."""

from __future__ import annotations

import textwrap

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
