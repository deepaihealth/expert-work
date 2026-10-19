"""Structure-aware Markdown chunking for the J.5 knowledge pipeline.

A parsed document (Markdown text) is sliced into retrieval chunks that:

* respect Markdown block structure — a chunk never cuts mid-paragraph
  or mid-table (markdown-it parses the block grammar);
* are bounded by a **token** budget (tiktoken-counted — char counts
  mislead badly for CJK);
* never straddle two document sections — a heading change flushes the
  current chunk;
* carry their **heading path** — the breadcrumb of the section a chunk came
  from travels with it (:class:`TextChunk`), so a short factual chunk is
  still findable ("免赔额是 500 元" is embedded / indexed as
  "[Section: 健康保险 > 免赔额]" + body) while the stored body stays clean;
* **overlap** within a section so content near a chunk boundary is not
  lost;
* keep tables whole, splitting an oversized table by rows (header
  retained) and an oversized paragraph on sentence boundaries.

:func:`chunk_markdown` is the structural chunker; :func:`chunk_markdown_semantic`
adds semantic refinement — it embeds blocks and additionally splits a
section at topic shifts (adjacent blocks whose embeddings are far apart),
so a long heading-less section is split where its subject actually
changes rather than at an arbitrary size point — but never right after a
fragment shorter than :data:`MIN_SEMANTIC_CHUNK_TOKENS` (a title or question
line, a page number, an image), which joins the chunk that follows. See
``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal
from uuid import UUID

import tiktoken
from markdown_it import MarkdownIt

from expert_work.protocol import chunk_retrieval_text
from orchestrator.llm import Embedder

#: tiktoken encoding for the token budget. An approximation of the
#: embedding model's tokenizer — exact enough for *sizing* chunks.
_ENCODING_NAME = "cl100k_base"
_encoding = tiktoken.get_encoding(_ENCODING_NAME)

#: markdown-it block tokens (at nesting level 0) that open a content block.
_CONTENT_OPENERS = frozenset(
    {"paragraph_open", "bullet_list_open", "ordered_list_open", "blockquote_open"}
)
#: markdown-it standalone block tokens that are content.
_CONTENT_STANDALONE = frozenset({"fence", "code_block", "html_block"})

#: Line breaks as markdown-it counts them (its ``normalize`` rule). Blocks are
#: cut out of the source by markdown-it's token line numbers, so the source
#: must be split the same way: ``str.splitlines`` also breaks on form feed,
#: vertical tab, \x1c to \x1e, NEL and U+2028 / U+2029, and one of those
#: shifted every later block onto the wrong lines — text silently lost (B-181).
_LINE_BREAK = re.compile(r"\r\n?|\n")

#: Sentence-ish boundary for hard-splitting an oversized paragraph. Zero
#: width, so splitting is lossless: after a CJK sentence mark always (CJK
#: prose has no space after 。), after an ASCII mark or a newline only when
#: whitespace follows. The CJK marks are intentional (noqa: ambiguous-unicode).
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？])|(?<=[.!?\n])(?=\s)")  # noqa: RUF001

#: Where an overlap tail may start: a paragraph break, a newline, or the
#: position right after a sentence mark. The earliest one inside the tail
#: window wins, so the carried context is clean *and* as long as possible.
_OVERLAP_START = re.compile(r"\n\n|\n|(?<=[。！？.!?])")  # noqa: RUF001

#: Cosine distance above which two adjacent blocks in the same section
#: are treated as a topic shift — a semantic chunk boundary. Conservative
#: so coherent prose is not over-fragmented; only genuine shifts split.
_SEMANTIC_BREAK_THRESHOLD = 0.4

#: A topic shift is a chunk boundary only once the chunk being built holds
#: at least this many tokens. A line on its own — a bold title, a question
#: heading its answer, a page number, an image — embeds far from both
#: neighbours, so it would otherwise become a chunk of its own: about 29% of
#: chunks in a 42k-chunk real corpus were such fragments, and a chunk holding
#: only a question outranks its answer on keyword match.
MIN_SEMANTIC_CHUNK_TOKENS = 32


def count_tokens(text: str) -> int:
    """Token count of ``text`` under the chunking tokenizer."""
    return len(_encode(text))


def _encode(text: str) -> list[int]:
    """Token ids of ``text``. Special-token strings such as ``<|endoftext|>``
    are ordinary document text here: a page about LLMs quotes them, and
    tiktoken's default refuses them — which failed the whole ingest."""
    return _encoding.encode(text, disallowed_special=())


@dataclass(frozen=True)
class _Block:
    """One top-level Markdown block, tagged with its section path."""

    kind: Literal["content", "table"]
    text: str
    heading_trail: tuple[str, ...]


@dataclass(frozen=True)
class TextChunk:
    """One retrieval chunk: its clean ``body`` and the ``heading_path`` of the
    section it came from (outermost heading first)."""

    body: str
    heading_path: tuple[str, ...]

    @property
    def text(self) -> str:
        """Breadcrumb + body — what is embedded and keyword-indexed
        (:func:`expert_work.protocol.chunk_retrieval_text`)."""
        return chunk_retrieval_text(self.heading_path, self.body)


def chunk_markdown(markdown: str, *, max_tokens: int, overlap_tokens: int) -> list[TextChunk]:
    """Slice ``markdown`` into retrieval chunks — the structural chunker (no
    semantic refinement).

    ``max_tokens`` bounds a chunk's body; ``overlap_tokens`` of the
    previous chunk's tail is carried into the next chunk *within the
    same section*. Returns chunk texts in document order; empty input
    yields an empty list.
    """
    return _pack_blocks(
        _parse_blocks(markdown),
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        semantic_breaks=frozenset(),
    )


async def chunk_markdown_semantic(
    markdown: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    embedder: Embedder,
    tenant_id: UUID,
) -> list[TextChunk]:
    """Like :func:`chunk_markdown`, but additionally splits a section at
    *topic shifts* — adjacent blocks whose embeddings are far apart.

    This refines the weak spot of purely structural chunking: a long
    heading-less section otherwise sliced at arbitrary size points is
    instead split where its content actually changes subject.
    """
    # Oversized blocks (a whole spreadsheet sheet parses to one table block —
    # up to 85k characters in real material) are cut to the chunk cap *before*
    # they are embedded for topic detection, so no single embedding input ever
    # exceeds the cap (the embedding API rejects inputs past its token limit
    # and the whole document would fail).
    blocks = _presplit_blocks(_parse_blocks(markdown), max_tokens)
    semantic_breaks: frozenset[int] = frozenset()
    if len(blocks) >= 2:
        embeddings = await embedder.embed([block.text for block in blocks], tenant_id=tenant_id)
        semantic_breaks = _semantic_breaks(blocks, embeddings)
    return _pack_blocks(
        blocks,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        semantic_breaks=semantic_breaks,
    )


def _pack_blocks(
    blocks: list[_Block],
    *,
    max_tokens: int,
    overlap_tokens: int,
    semantic_breaks: frozenset[int],
) -> list[TextChunk]:
    """Pack blocks into chunks. Flush on a heading change, on a
    ``semantic_breaks`` index (a topic shift) once the chunk holds
    :data:`MIN_SEMANTIC_CHUNK_TOKENS`, and on the token cap."""
    chunks: list[TextChunk] = []
    trail: tuple[str, ...] = ()
    buffer: list[str] = []
    buffer_tokens = 0

    def flush(*, carry_overlap: bool) -> None:
        nonlocal buffer, buffer_tokens
        if not buffer:
            return
        body = "\n\n".join(buffer)
        chunks.append(TextChunk(body=body, heading_path=trail))
        tail = _tail_text(body, overlap_tokens) if carry_overlap else ""
        buffer = [tail] if tail.strip() else []
        buffer_tokens = count_tokens(tail) if buffer else 0

    for index, block in enumerate(blocks):
        if block.heading_trail != trail:
            flush(carry_overlap=False)
            trail = block.heading_trail
        elif index in semantic_breaks and buffer_tokens >= MIN_SEMANTIC_CHUNK_TOKENS:
            # Topic shift inside a section — a clean boundary, no overlap. A
            # shorter fragment stays and opens the next chunk instead.
            flush(carry_overlap=False)
        for piece in _prepare_block(block, max_tokens):
            piece_tokens = count_tokens(piece)
            if buffer and buffer_tokens + piece_tokens > max_tokens:
                flush(carry_overlap=True)
            buffer.append(piece)
            buffer_tokens += piece_tokens
    flush(carry_overlap=False)
    return chunks


def _semantic_breaks(blocks: list[_Block], embeddings: Sequence[Sequence[float]]) -> frozenset[int]:
    """Block indices that start a new semantic segment — adjacent
    same-section blocks whose embeddings exceed the distance threshold.
    A heading change is already a flush point, so it is skipped here."""
    breaks: set[int] = set()
    for i in range(1, len(blocks)):
        if blocks[i].heading_trail != blocks[i - 1].heading_trail:
            continue
        if _cosine_distance(embeddings[i - 1], embeddings[i]) >= _SEMANTIC_BREAK_THRESHOLD:
            breaks.add(i)
    return frozenset(breaks)


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine distance (0 = identical) between two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


def _parse_blocks(markdown: str) -> list[_Block]:
    """Parse ``markdown`` into top-level content / table blocks, each
    tagged with the heading breadcrumb of the section it sits in."""
    tokens = MarkdownIt().enable("table").parse(markdown)
    lines = _LINE_BREAK.split(markdown)
    blocks: list[_Block] = []
    trail_stack: list[tuple[int, str]] = []
    for index, token in enumerate(tokens):
        if token.level != 0 or token.map is None:
            continue
        if token.type == "heading_open":
            level = int(token.tag[1:])
            text = tokens[index + 1].content.strip() if index + 1 < len(tokens) else ""
            while trail_stack and trail_stack[-1][0] >= level:
                trail_stack.pop()
            trail_stack.append((level, text))
            continue
        start, end = token.map
        block_text = "\n".join(lines[start:end]).strip()
        if not block_text:
            continue
        trail = tuple(text for _, text in trail_stack)
        if token.type == "table_open":
            blocks.append(_Block("table", block_text, trail))
        elif token.type in _CONTENT_OPENERS or token.type in _CONTENT_STANDALONE:
            blocks.append(_Block("content", block_text, trail))
    return blocks


def _presplit_blocks(blocks: list[_Block], max_tokens: int) -> list[_Block]:
    """Cut every block over ``max_tokens`` into cap-sized pieces that keep its
    kind and heading trail; blocks within the cap pass through untouched."""
    return [
        _Block(block.kind, piece, block.heading_trail)
        for block in blocks
        for piece in _prepare_block(block, max_tokens)
    ]


def _prepare_block(block: _Block, max_tokens: int) -> list[str]:
    """Return ``block`` as one or more pieces, each within ``max_tokens``."""
    if count_tokens(block.text) <= max_tokens:
        return [block.text]
    if block.kind == "table":
        return _split_table(block.text, max_tokens)
    return _hard_split(block.text, max_tokens)


def _split_table(table: str, max_tokens: int) -> list[str]:
    """Split an oversized Markdown table by rows, repeating the header +
    separator rows in every part so each part is a valid table."""
    lines = _LINE_BREAK.split(table)
    if len(lines) <= 2:  # header + separator only — nothing to split
        return [table]
    header = "\n".join(lines[:2])
    parts: list[str] = []
    current: list[str] = []
    for row in lines[2:]:
        candidate = "\n".join([header, *current, row])
        if current and count_tokens(candidate) > max_tokens:
            parts.append("\n".join([header, *current]))
            current = [row]
        else:
            current.append(row)
    if current:
        parts.append("\n".join([header, *current]))
    return parts


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Split oversized prose on sentence boundaries, packing to the cap;
    a single sentence still over the cap is split by raw tokens."""
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in _SENTENCE_BOUNDARY.split(text):
        if not unit:
            continue
        unit_tokens = count_tokens(unit)
        if unit_tokens > max_tokens:
            if current:
                parts.append("".join(current).strip())
                current, current_tokens = [], 0
            parts.extend(_split_by_tokens(unit, max_tokens))
            continue
        if current and current_tokens + unit_tokens > max_tokens:
            parts.append("".join(current).strip())
            current, current_tokens = [], 0
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        parts.append("".join(current).strip())
    return parts


def _split_by_tokens(text: str, max_tokens: int) -> list[str]:
    """Last-resort split of an unbreakable run on token windows, cut at
    character offsets so a multi-byte character is never torn in two
    (a plain ``decode`` of a token slice yields U+FFFD for CJK)."""
    token_ids = _encode(text)
    decoded, offsets = _encoding.decode_with_offsets(token_ids)
    bounds = [offsets[i] for i in range(0, len(token_ids), max_tokens)] + [len(decoded)]
    return [decoded[a:b] for a, b in pairwise(bounds) if decoded[a:b]]


def _tail_text(text: str, overlap_tokens: int) -> str:
    """The overlap carried into the next chunk: the last ``overlap_tokens``
    tokens of ``text``, cut at a character offset and then advanced to the
    earliest paragraph / line / sentence boundary inside that window."""
    if overlap_tokens <= 0:
        return ""
    token_ids = _encode(text)
    if len(token_ids) <= overlap_tokens:
        return text
    decoded, offsets = _encoding.decode_with_offsets(token_ids)
    return _start_at_boundary(decoded[offsets[-overlap_tokens] :])


def _start_at_boundary(tail: str) -> str:
    """Drop the partial sentence a token window starts with; keep the whole
    window when it holds no boundary at all."""
    for match in _OVERLAP_START.finditer(tail):
        rest = tail[match.end() :].lstrip()
        if rest:
            return rest
    return tail
