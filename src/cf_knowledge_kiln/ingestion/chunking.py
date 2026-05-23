"""Structure-aware Markdown chunking (#15).

Pipeline:

1. Frontmatter is extracted via :mod:`frontmatter` and exposed as a
   dict so callers can apply per-document overrides over the source's
   defaults.
2. The body is split into *atomic blocks* by a line-based scanner that
   recognizes headings, fenced code blocks, GFM tables, list groups,
   blank lines, and paragraphs. Atomic blocks are never split across
   chunks — this is what "preserve tables/code/lists" means.
3. Blocks are grouped into *sections* bounded by headings. Each
   section's chunk inherits the heading path (the chain of headings
   from the document root to the current section).
4. If a section's token count exceeds the target maximum, the section
   is repacked into chunks by greedy block-aggregation: blocks are
   added one at a time until the next block would push past the cap,
   at which point a chunk is emitted. Single atomic blocks larger than
   the cap are emitted as their own oversized chunk (rare; surfaces in
   the ingestion summary so an operator can decide whether to refine
   the source).

We use :mod:`mistune` to parse and validate the body — if the body
isn't valid Markdown, the call returns no chunks and the caller
records ``parse_error`` in the ingestion summary. Mistune's AST is
**not** used to drive chunk boundaries (line-based scanning gives us
position information we'd otherwise lose when re-rendering AST nodes).

Known limitations of the line-based scanner (acceptable today; revisit
in Phase 4 if retrieval quality suffers):

* **ATX headings only** (``# Title``). Setext-style headings
  (``Title\\n====``) fall through to the paragraph branch and the H1 is
  not added to ``heading_path``. Internal docs use ATX consistently.
* **Blockquotes** (``> ...``) are absorbed into paragraphs.
* **Single-column GFM tables** are not recognized as tables (multi-
  column only; rare in practice).
* Orphan H3 (an ``### Foo`` with no preceding H1/H2) produces a
  ``heading_path`` like ``["Foo"]``, indistinguishable from an H1.
  Source documents should anchor on H1 to avoid the ambiguity.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import frontmatter
import mistune

from cf_knowledge_kiln.ingestion._jsonsafe import jsonify
from cf_knowledge_kiln.ingestion.tokens import count_tokens

# Token-target window for a chunk. Per the plan: roughly 300-800 tokens.
DEFAULT_MAX_TOKENS = 800
DEFAULT_MIN_TOKENS = 300

# Defensive cap on YAML frontmatter size (#54). Stops a malicious or
# accidentally-massive source doc from pushing a multi-megabyte blob
# into the documents.metadata JSONB column. 100 KiB is loose enough
# that a real ADR-style doc is unaffected and tight enough that a
# bug or attack surfaces immediately.
MAX_FRONTMATTER_BYTES = 100 * 1024


class FrontmatterTooLargeError(ValueError):
    """Raised when a doc's YAML frontmatter exceeds :data:`MAX_FRONTMATTER_BYTES`."""


_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*$")
_FENCE_OPEN_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


@dataclass(frozen=True)
class Chunk:
    """One chunk from a document; matches ``document_chunks`` columns."""

    content: str
    heading_path: list[str]
    chunk_index: int
    content_tokens: int
    content_hash: str


@dataclass(frozen=True)
class ParsedDocument:
    """A parsed Markdown document.

    ``meta`` is the frontmatter dict (empty if absent). ``chunks`` is
    ordered by appearance. ``title`` falls back to the first H1 if
    frontmatter doesn't set one.
    """

    meta: dict[str, Any]
    chunks: list[Chunk]
    title: str | None = None


@dataclass
class _Block:
    """One atomic block of Markdown."""

    kind: str  # heading | code | table | list | paragraph
    text: str  # original lines, joined with newlines, no trailing newline
    heading_level: int = 0
    heading_text: str = ""


def _scan_blocks(body: str) -> list[_Block]:
    """Split ``body`` into atomic blocks. Blank lines collapse separators."""
    lines = body.splitlines()
    blocks: list[_Block] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        # Fenced code block — close fence must be the SAME character class
        # and AT LEAST as long as the opener (CommonMark §4.5). This lets
        # docs use ```` ```` to wrap a block that itself contains ``` lines.
        fence_m = _FENCE_OPEN_RE.match(line)
        if fence_m:
            fence = fence_m.group("fence")
            fence_char = fence[0]
            fence_len = len(fence)
            start = i
            i += 1
            while i < n:
                stripped = lines[i].lstrip()
                if stripped.startswith(fence_char * fence_len) and set(
                    stripped[: len(stripped.rstrip())]
                ) <= {fence_char}:
                    i += 1  # consume closing fence
                    break
                i += 1
            blocks.append(_Block(kind="code", text="\n".join(lines[start:i])))
            continue
        heading_m = _HEADING_RE.match(line)
        if heading_m:
            level = len(heading_m.group("hashes"))
            text = heading_m.group("text").strip()
            blocks.append(_Block(kind="heading", text=line, heading_level=level, heading_text=text))
            i += 1
            continue
        # GFM table: header line + separator line.
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            start = i
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                i += 1
            blocks.append(_Block(kind="table", text="\n".join(lines[start:i])))
            continue
        # List group: contiguous list items + their indented continuations.
        if _LIST_ITEM_RE.match(line):
            start = i
            i += 1
            while i < n and (
                _LIST_ITEM_RE.match(lines[i])
                or lines[i].startswith((" ", "\t"))
                or not lines[i].strip()
            ):
                if not lines[i].strip() and i + 1 < n and not _LIST_ITEM_RE.match(lines[i + 1]):
                    break  # blank line followed by non-list ends the group
                i += 1
            blocks.append(_Block(kind="list", text="\n".join(lines[start:i])))
            continue
        # Paragraph: until a blank line or a block-starting line.
        start = i
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i], lines, i):
            i += 1
        blocks.append(_Block(kind="paragraph", text="\n".join(lines[start:i])))
    return blocks


def _is_block_start(line: str, lines: list[str], idx: int) -> bool:
    if _HEADING_RE.match(line):
        return True
    if _FENCE_OPEN_RE.match(line):
        return True
    if _LIST_ITEM_RE.match(line):
        return True
    return bool("|" in line and idx + 1 < len(lines) and _TABLE_SEP_RE.match(lines[idx + 1]))


@dataclass
class _Section:
    heading_path: list[str] = field(default_factory=list)
    blocks: list[_Block] = field(default_factory=list)


def _group_into_sections(blocks: list[_Block]) -> list[_Section]:
    """Group blocks into sections bounded by headings. Maintains heading-path stack.

    #201: a section whose only block is its heading (e.g. an ``H2`` with
    no preamble before a nested ``H3``, or a top-level ``H1`` followed
    immediately by ``H2``s) is skipped — emitting it produces a 2-5
    token \"## Heading\" chunk that pollutes the index, burns embedding
    storage, and lets a literal heading match boost a near-empty chunk
    above richer siblings. The heading is still in the
    ``heading_path`` of every descendant section, so no information
    is lost; the standalone stub is the thing dropped.
    """
    sections: list[_Section] = []
    path: list[str] = []
    current = _Section(heading_path=[])
    for block in blocks:
        if block.kind == "heading":
            if _has_body(current):
                sections.append(current)
            path = path[: block.heading_level - 1]
            path.append(block.heading_text)
            current = _Section(heading_path=list(path), blocks=[block])
        else:
            current.blocks.append(block)
    if _has_body(current):
        sections.append(current)
    return sections


def _has_body(section: _Section) -> bool:
    """True iff the section contains at least one non-heading block.

    #201 — a section whose blocks are only its heading is content-free
    once the heading_path is preserved on descendant sections; emitting
    it as its own chunk just pollutes the index.
    """
    return any(b.kind != "heading" for b in section.blocks)


def _section_text(section: _Section) -> str:
    return "\n\n".join(b.text for b in section.blocks).strip()


def _pack_blocks(blocks: list[_Block], max_tokens: int) -> Iterator[tuple[str, int]]:
    """Greedy-pack blocks into chunks under ``max_tokens``.

    A single block bigger than ``max_tokens`` is emitted as its own
    chunk (oversized but atomic). Heading blocks act as soft starts —
    they go with the next batch, not alone.
    """
    buf: list[_Block] = []
    buf_tokens = 0
    for block in blocks:
        block_text = block.text
        block_tokens = count_tokens(block_text)
        if buf and buf_tokens + block_tokens > max_tokens and block.kind != "heading":
            yield "\n\n".join(b.text for b in buf).strip(), buf_tokens
            buf = []
            buf_tokens = 0
        buf.append(block)
        buf_tokens += block_tokens
    if buf:
        yield "\n\n".join(b.text for b in buf).strip(), buf_tokens


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_chunk(text: str, path: list[str], idx: int, tokens: int) -> Chunk:
    return Chunk(
        content=text,
        heading_path=list(path),
        chunk_index=idx,
        content_tokens=tokens,
        content_hash=_content_hash(text),
    )


def _check_frontmatter_size(source_text: str) -> None:
    """Reject docs whose frontmatter exceeds :data:`MAX_FRONTMATTER_BYTES`.

    Frontmatter is delimited by a leading ``---`` and a closing ``---``
    on its own line. We measure just the bytes between those markers
    so a 100 MB body doesn't trip the check.
    """
    text = source_text.lstrip()
    if not text.startswith("---"):
        return
    # Find the closing fence on its own line.
    after_open = text[3:].lstrip("\r\n")
    end_marker_idx = after_open.find("\n---")
    if end_marker_idx == -1:
        return
    fm_bytes = len(after_open[:end_marker_idx].encode("utf-8"))
    if fm_bytes > MAX_FRONTMATTER_BYTES:
        raise FrontmatterTooLargeError(
            f"frontmatter is {fm_bytes} bytes; max {MAX_FRONTMATTER_BYTES}"
        )


def parse_document(
    source_text: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_tokens: int = DEFAULT_MIN_TOKENS,  # noqa: ARG001 - advisory floor; reserved for future
) -> ParsedDocument:
    """Parse Markdown ``source_text`` into a :class:`ParsedDocument`.

    Returns an empty chunk list if the body doesn't parse - the caller
    should record ``parse_error`` in the ingestion summary.
    ``min_tokens`` is reserved as a soft floor for future merging of
    very-small adjacent sections; today we prefer respecting heading
    boundaries over hitting a minimum.
    """
    # Defensive size cap (#54): a malicious source doc could ship a
    # 10 MB nested YAML frontmatter that becomes documents.metadata
    # JSONB. Reject before the YAML parser even sees it so the worst
    # case is one bounded read instead of an arbitrarily large parse.
    _check_frontmatter_size(source_text)
    fm = frontmatter.loads(source_text)
    body = fm.content
    # YAML safe_load returns native Python types (date, datetime, UUID,
    # Decimal). The documents.metadata column is JSONB, so any non-
    # JSON-native value here would crash the upsert (#91). Normalize at
    # the parser boundary so downstream code sees only JSON-safe values.
    meta: dict[str, Any] = jsonify(dict(fm.metadata))
    try:
        mistune.create_markdown()(body)
    except Exception:  # pragma: no cover - mistune is permissive
        return ParsedDocument(meta=meta, chunks=[], title=None)

    blocks = _scan_blocks(body)
    sections = _group_into_sections(blocks)

    chunks: list[Chunk] = []
    title: str | None = meta.get("title")
    for section in sections:
        text = _section_text(section)
        if not text:
            continue
        if title is None:
            for b in section.blocks:
                if b.kind == "heading" and b.heading_level == 1:
                    title = b.heading_text
                    break
        section_tokens = count_tokens(text)
        if section_tokens <= max_tokens:
            chunks.append(_make_chunk(text, section.heading_path, len(chunks), section_tokens))
            continue
        for sub_text, sub_tokens in _pack_blocks(section.blocks, max_tokens):
            chunks.append(_make_chunk(sub_text, section.heading_path, len(chunks), sub_tokens))

    # min_tokens is an advisory floor — we surface very-small chunks in
    # logs but do not merge them across heading boundaries.
    return ParsedDocument(meta=meta, chunks=chunks, title=title)
