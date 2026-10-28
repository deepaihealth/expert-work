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
  lost — the base's overlap, and regardless of it the last sentence of a
  paragraph that had to be cut mid-text (B-206 K-3);
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

Chunks are the children of **parent chunks** (B-206 K-1,
:func:`chunk_markdown_parented`): a search matches a chunk and returns its
parent, a run of whole blocks within :data:`PARENT_MAX_TOKENS` that follows
chapters where it can. Parents are cut first, chunks inside them — a chunk
never crosses a parent.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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

#: Tokens the ``\n\n`` between two packed pieces costs (at most; it can merge
#: with a neighbour). Counted so the packed body stays within its budget.
_JOIN_TOKENS = 1

#: Longest heading taken as a section title; a longer one is kept as content.
#: A 17 KB "heading" (a whole meeting transcript above a ``---`` line, read as
#: a setext heading) put its text in no chunk body and on every chunk's
#: breadcrumb (B-198); setext headings are off now (B-199), and this bounds
#: an ATX ``#`` line the same way.
_MAX_HEADING_CHARS = 200

#: Sentence-ish boundary for hard-splitting an oversized paragraph. Zero
#: width, so splitting is lossless: after a CJK sentence mark always (CJK
#: prose has no space after 。), after an ASCII mark or a newline only when
#: whitespace follows. The CJK marks are intentional (noqa: ambiguous-unicode).
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？])|(?<=[.!?\n])(?=\s)")  # noqa: RUF001

#: Where an overlap tail may start: a paragraph break, a newline, or the
#: position right after a sentence mark. The earliest one inside the tail
#: window wins, so the carried context is clean *and* as long as possible.
_OVERLAP_START = re.compile(r"\n\n|\n|(?<=[。！？.!?])")  # noqa: RUF001

#: Lines that open a new part of a text (B-206 K-3): an oversized block is cut
#: in front of them before anywhere else — a numbered line ("1." / "2.3" with
#: a CJK enumeration comma / a CJK numeral with one / a parenthesised CJK
#: numeral), a chapter or article line ("di san zhang", "di 2 tiao"), a rule
#: line (``---``). A page break (form feed) is a cut point too. Only a block
#: that has to be cut is: one within the cap keeps every character in place.
#: Full-width marks are escapes: U+3001 enumeration comma, U+FF08 / U+FF09
#: parentheses (a lint autofix folds the literal characters to ASCII).
_CUT_LINE = re.compile(
    r"^[ \t]*(?:"
    r"\d+(?:\.\d+)*[.\u3001)\uff09](?!\d)"
    r"|[\uff08(]?[一二三四五六七八九十百]+[\u3001.)\uff09]"
    r"|第[ \t]*[0-9一二三四五六七八九十百千]+[ \t]*[章节条篇部卷]"
    r"|(?:-{3,}|\*{3,}|_{3,}|={3,})[ \t]*$"
    r")",
    re.MULTILINE,
)

#: A speaker label opening a line: a name of up to three words and a colon
#: (ASCII or full-width U+FF1A), after an optional bracketed time stamp as
#: chat exports write it ("[2026-04-02 10:02] Sales Lin:"). A block whose
#: lines mostly open with one is a conversation, cut between lines (B-206 K-3).
_SPEAKER = re.compile(
    r"^[ \t]*(?:[\[\u3010][^\]\u3011\n]{1,40}[\]\u3011][ \t]*)?"
    r"[^\s:\uff1a\[\]]{1,24}(?:[ \t][^\s:\uff1a]{1,24}){0,2}[:\uff1a]"
)

#: A block of at least :data:`_LINE_BLOCK_MIN_LINES` lines whose median line is
#: shorter than this is a list of lines (one utterance, item or entry each),
#: cut between lines rather than at sentence marks inside them.
_SHORT_LINE_CHARS = 40
_LINE_BLOCK_MIN_LINES = 3

#: A line that introduces what follows it ends in a question mark or a colon
#: (ASCII or full-width U+FF1F / U+FF1A) once bold / italic marks are off.
_LEAD_IN_END = re.compile(r"[?:\uff1f\uff1a]$")

#: Share of the cap a paragraph cut mid-text repeats from the end of the piece
#: before (FastGPT: overlap only at a cut inside a paragraph; Unstructured
#: ``overlap``: only between pieces of a split element). Cuts at structure —
#: a heading, a blank line, a numbered or chapter line — repeat nothing.
_CUT_OVERLAP_PERCENT = 15

#: One line with its line break (the breaks markdown-it counts, ``_LINE_BREAK``).
_LINE_WITH_END = re.compile(r"[^\r\n]*(?:\r\n?|\n)|[^\r\n]+$")

#: A Markdown table's header separator row (``| --- | :-: |``).
_TABLE_SEPARATOR_ROW = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-+:?[ \t]*\|)+[ \t]*(?::?-+:?[ \t]*)?$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")

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

#: A section whose only chunk holds fewer tokens than this — a heading with
#: one line under it, a page number, a heading with nothing under it — joins
#: a neighbouring chunk of its parent section instead of standing alone
#: (B-206 K-3). Low on purpose: at 100 a focused 80-token section went into a
#: bigger neighbour and lost its rank to chunks about only its subject (dev
#: set); RAGFlow joins across sections only under 32 tokens, Docling never.
SHORT_SECTION_TOKENS = 50

#: Blocks embedded per topic-detection window. Only adjacent blocks are
#: compared, so each window's vectors are dropped once compared; only the
#: last one is carried into the next window. Holding every block's vector
#: (1,024 floats, ~40 KiB as Python objects) at once took the server 600 MiB
#: for a 5 MB text file. A multiple of the usual embedding batch sizes (10,
#: 25, 50, 100), so the requests sent stay the same.
SEMANTIC_EMBED_WINDOW = 200

#: Cap of a parent chunk (B-206 K-1), measured as a child is — document title,
#: breadcrumb and body together. A constant for now, not a base setting: a
#: parent is what a search returns, the size answers were found to need.
PARENT_MAX_TOKENS = 1024

#: Tokens under the parent cap within which a run's size is measured rather
#: than estimated (:meth:`_ParentCut._grown`).
_ESTIMATE_SLACK = 16

#: Headings of this level or above (``#``, ``##``) are chapters: a new one
#: closes a parent that is at least half full, so parents follow chapters
#: where a document has them (B-206 K-1).
_CHAPTER_LEVEL = 2

#: Version of the chunking rules, part of every :func:`chunk_params_hash`.
#: Bump it with a change that cuts the same text differently under the same
#: parameters, so documents chunked by the old rules show as an old version
#: to re-ingest (B-206 K-3; "k3k1": children cut inside parents, K-1).
CHUNKER_VERSION = "k3k1"

#: Whether an ingest also splits sections at topic shifts
#: (:func:`chunk_markdown_semantic`, ``Settings.knowledge_semantic_breaks``).
#: On by default. B-206 K-3 turned it off (published comparisons found no
#: stable gain, and finding the shifts embeds every block once more on every
#: ingest); the dev set then lost on real staff questions — rerank MRR@10
#: 0.755 off vs 0.783 on, section hit 0.934 vs 0.961 — for about 1.8x the
#: ingest embedding cost (2026-10-25). The switch turns it off to save that.
_semantic_breaks_on = True


def configure_semantic_breaks(*, enabled: bool) -> None:
    """Turn topic-shift splitting on or off for the ingests that follow."""
    global _semantic_breaks_on
    _semantic_breaks_on = enabled


def semantic_breaks_enabled() -> bool:
    """Whether ingests split sections at topic shifts (on by default)."""
    return _semantic_breaks_on


def chunk_params_hash(
    *, max_tokens: int, overlap_tokens: int, parent_max_tokens: int = PARENT_MAX_TOKENS
) -> str:
    """Fingerprint of the chunking a document's chunks come from: the rules
    (:data:`CHUNKER_VERSION`), the base's size and overlap, the parent cap, and
    whether topic shifts split. A document whose stored fingerprint differs
    from its base's current one was cut by other parameters and is due for a
    re-ingest."""
    mode = "semantic" if _semantic_breaks_on else "structural"
    key = f"{CHUNKER_VERSION}|{max_tokens}|{overlap_tokens}|{parent_max_tokens}|{mode}"
    return hashlib.sha256(key.encode("ascii")).hexdigest()[:16]


def count_tokens(text: str) -> int:
    """Token count of ``text`` under the chunking tokenizer."""
    return len(_encode(text))


def _encode(text: str) -> list[int]:
    """Token ids of ``text``. Special-token strings such as ``<|endoftext|>``
    are ordinary document text here: a page about LLMs quotes them, and
    tiktoken's default refuses them — which failed the whole ingest."""
    return _encoding.encode(text, disallowed_special=())


@dataclass
class _Heading:
    """An open section while parsing: ``used`` once any block sits under it,
    so a heading with no content can be kept as content itself."""

    level: int
    text: str
    used: bool = False


@dataclass(frozen=True)
class _Block:
    """One top-level Markdown block, tagged with its section path and the
    ordinal of the level-1 / level-2 heading it sits under (``chapter``: a new
    one may close a parent early, B-206 K-1)."""

    kind: Literal["content", "table"]
    text: str
    heading_trail: tuple[str, ...]
    chapter: int = 0


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


@dataclass(frozen=True)
class ParentChunk:
    """One parent chunk (B-206 K-1): what a search returns when one of its
    ``children`` — the chunks searched — matches. Its ``body`` is a run of
    whole blocks of the document, each written once, with the headings the
    run enters written in front of their text; its ``heading_path`` is its
    first child's."""

    body: str
    heading_path: tuple[str, ...]
    children: tuple[TextChunk, ...]

    @property
    def is_trivial(self) -> bool:
        """Whether the parent is just its one child (same body, same heading
        path): no parent row is stored then, the child stands for itself."""
        return len(self.children) == 1 and self.children[0] == TextChunk(
            body=self.body, heading_path=self.heading_path
        )


@dataclass
class _Section:
    """The chunks packed from one run of blocks under the same heading trail."""

    trail: tuple[str, ...]
    chunks: list[TextChunk] = field(default_factory=list)


@dataclass(frozen=True)
class _Unit:
    """A chunk on its way out of :func:`_join_short_sections`: the full
    heading ``trail`` of its (first) section and whether it is that section's
    ``whole`` text."""

    chunk: TextChunk
    trail: tuple[str, ...]
    whole: bool


@dataclass
class BreakMemo:
    """Topic-shift decisions of adjacent block pairs, kept between ingests of
    one document (B-215): a re-ingest whose text is unchanged — re-ingest,
    「补识别」 with nothing newly read — embeds no block for topic detection,
    and an edited document embeds only the blocks next to an edit. A decision
    depends on the two blocks' texts, the embedding model (``namespace``) and
    the threshold, so the key covers all of them.

    ``known`` is what an earlier ingest decided; :func:`chunk_markdown_semantic`
    fills ``decided`` with every pair of this text (reused or fresh), which is
    what the caller stores back — pairs the text no longer has drop out."""

    namespace: str
    known: Mapping[str, bool] = field(default_factory=dict)
    decided: dict[str, bool] = field(default_factory=dict)

    def key(self, previous: str, current: str) -> str:
        # Lengths first: no two different pairs (nor namespaces) join the same.
        parts = (self.namespace, str(_SEMANTIC_BREAK_THRESHOLD), previous, current)
        joined = "".join(f"{len(part)}:{part}" for part in parts)
        return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def chunk_markdown(
    markdown: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    plain_text: bool = False,
    doc_title: str | None = None,
) -> list[TextChunk]:
    """Slice ``markdown`` into retrieval chunks — the structural chunker (no
    semantic refinement): the children of :func:`chunk_markdown_parented`, in
    document order.

    ``max_tokens`` bounds a chunk's whole embedded text — ``doc_title`` (the
    title its retrieval text will carry), breadcrumb and body together;
    ``overlap_tokens`` of the previous chunk's tail is carried into the next
    chunk *within the same section*. ``plain_text`` reads the input as plain
    text, not Markdown (a ``.txt`` file). Returns chunk texts in document
    order; empty input yields an empty list.
    """
    parents = chunk_markdown_parented(
        markdown,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        plain_text=plain_text,
        doc_title=doc_title,
    )
    return [child for parent in parents for child in parent.children]


def chunk_markdown_parented(
    markdown: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    plain_text: bool = False,
    doc_title: str | None = None,
    parent_max_tokens: int = PARENT_MAX_TOKENS,
) -> list[ParentChunk]:
    """``markdown`` as parent chunks, each holding its retrieval chunks (B-206
    K-1). Parents are cut first and children inside them (Dify's order), so a
    child never crosses a parent and a parent repeats nothing its children
    repeat — a table header, a question line, an overlap.

    A parent is a run of whole blocks within ``parent_max_tokens``, measured
    as a child is (title, breadcrumb and body): the whole document when it
    fits; else the run closes where the next block does not fit, or at a
    level-1 / level-2 heading once at least half full, and never right after a
    line introducing the next block of its section (that line opens the next
    parent with it). A block over the parent cap on its own is cut to parent
    size first — prose where a cut does least harm, a table by rows under its
    header, a long row with its leading cells. The children are what
    :func:`chunk_markdown`'s rules make of each parent's blocks."""
    cut = _ParentCut(
        max_tokens=max_tokens, parent_max_tokens=parent_max_tokens, doc_title=doc_title
    )
    written: set[tuple[str, ...]] = set()
    return [
        cut.parent(
            group,
            _pack_blocks(
                group,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                semantic_breaks=frozenset(),
                doc_title=doc_title,
                written=written,
            ),
        )
        for group in cut.groups(_blocks(markdown, plain_text=plain_text))
    ]


async def chunk_markdown_semantic(
    markdown: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    embedder: Embedder,
    tenant_id: UUID,
    plain_text: bool = False,
    doc_title: str | None = None,
    memo: BreakMemo | None = None,
) -> list[TextChunk]:
    """Like :func:`chunk_markdown`, but additionally splits a section at
    *topic shifts* — adjacent blocks whose embeddings are far apart: the
    children of :func:`chunk_markdown_semantic_parented`, in document order.

    This refines the weak spot of purely structural chunking: a long
    heading-less section otherwise sliced at arbitrary size points is
    instead split where its content actually changes subject. ``memo``
    reuses earlier decisions (see :class:`BreakMemo`).
    """
    parents = await chunk_markdown_semantic_parented(
        markdown,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        embedder=embedder,
        tenant_id=tenant_id,
        plain_text=plain_text,
        doc_title=doc_title,
        memo=memo,
    )
    return [child for parent in parents for child in parent.children]


async def chunk_markdown_semantic_parented(
    markdown: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    embedder: Embedder,
    tenant_id: UUID,
    plain_text: bool = False,
    doc_title: str | None = None,
    memo: BreakMemo | None = None,
    parent_max_tokens: int = PARENT_MAX_TOKENS,
) -> list[ParentChunk]:
    """The parents of :func:`chunk_markdown_parented`, their children also
    split at topic shifts (:func:`chunk_markdown_semantic`). A shift only
    splits inside a parent: parents are cut by structure and size alone."""
    cut = _ParentCut(
        max_tokens=max_tokens, parent_max_tokens=parent_max_tokens, doc_title=doc_title
    )
    groups = cut.groups(_blocks(markdown, plain_text=plain_text))
    # Oversized blocks (a whole spreadsheet sheet parses to one table block —
    # up to 85k characters in real material) are cut to the chunk cap *before*
    # they are embedded for topic detection, so no single embedding input ever
    # exceeds the cap (the embedding API rejects inputs past its token limit
    # and the whole document would fail).
    split = [_presplit_blocks(group, max_tokens) for group in groups]
    blocks = [block for group in split for block in group]
    semantic_breaks: frozenset[int] = frozenset()
    if len(blocks) >= 2:
        semantic_breaks = await _semantic_breaks(blocks, embedder, tenant_id, memo)
    parents: list[ParentChunk] = []
    written: set[tuple[str, ...]] = set()
    start = 0  # index in the document of the parent's first block
    for group, pieces in zip(groups, split, strict=True):
        breaks = frozenset(
            index for index in range(1, len(pieces)) if start + index in semantic_breaks
        )
        start += len(pieces)
        children = _pack_blocks(
            pieces,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            semantic_breaks=breaks,
            doc_title=doc_title,
            written=written,
        )
        parents.append(cut.parent(group, children))
    return parents


def structural_chunk_count(
    markdown: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    plain_text: bool = False,
    doc_title: str | None = None,
    parent_max_tokens: int = PARENT_MAX_TOKENS,
) -> int:
    """How many chunks :func:`chunk_markdown_semantic` makes with no topic
    shift at all — the same parents and blocks, no embedding call, nothing
    billed. Topic shifts only split further, so this is what a document costs
    at least (B-214 refuses a document over the chunk cap before it is
    embedded). Children only: a parent is stored, never embedded."""
    cut = _ParentCut(
        max_tokens=max_tokens, parent_max_tokens=parent_max_tokens, doc_title=doc_title
    )
    written: set[tuple[str, ...]] = set()
    return sum(
        len(
            _pack_blocks(
                _presplit_blocks(group, max_tokens),
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                semantic_breaks=frozenset(),
                doc_title=doc_title,
                written=written,
            )
        )
        for group in cut.groups(_blocks(markdown, plain_text=plain_text))
    )


def normalize_passage_body(body: str) -> str:
    """A passage's text as it is stored when edited or added by hand (B-175
    P3-PR2): its paragraphs, each trimmed, between single blank lines — the
    text :func:`chunk_passage_body` makes one chunk of when it fits."""
    return "\n\n".join(block.text for block in _parse_plain_blocks(body))


def chunk_passage_body(
    body: str,
    heading_path: Sequence[str],
    *,
    max_tokens: int,
    overlap_tokens: int,
    doc_title: str | None,
) -> list[TextChunk]:
    """The chunks of one passage's text (B-175 P3-PR2): an edit, a passage
    added by hand, a re-ingest putting an edit back. The text is cut by the
    base's chunk rules inside the passage — paragraphs (blank-line separated;
    one whose every line is a table row is a table) packed to ``max_tokens``
    with ``overlap_tokens``, an oversized one cut like a document's — and every
    chunk carries the passage's breadcrumb ``heading_path``. Deterministic; one
    chunk when the text fits; none for blank text."""
    trail = tuple(heading_path)
    blocks = [
        replace(block, kind=_passage_block_kind(block.text), heading_trail=trail)
        for block in _parse_plain_blocks(body)
    ]
    if not blocks:
        return []
    # Every heading of the breadcrumb counts as written: none is put into a
    # chunk's body (the passage's text already holds the ones it shows).
    written = {trail[: depth + 1] for depth in range(len(trail))}
    chunks = _pack_blocks(
        blocks,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        semantic_breaks=frozenset(),
        doc_title=doc_title,
        written=written,
    )
    return [TextChunk(body=chunk.body, heading_path=trail) for chunk in chunks]


def _passage_block_kind(text: str) -> Literal["content", "table"]:
    lines = _LINE_BREAK.split(text)
    is_table = len(lines) > 1 and all(line.lstrip().startswith("|") for line in lines)
    return "table" if is_table else "content"


class _ParentCut:
    """The parents of one document (B-206 K-1): which blocks each holds
    (:meth:`groups`) and the parent a run of blocks and its children make
    (:meth:`parent`).

    A parent's breadcrumb is its first child's: its first section's, or — a
    short first section without one joins the next (:func:`_join`) — the
    first section's that has one. A run is measured with the larger of the
    two, so the parent holds within the cap either way."""

    def __init__(self, *, max_tokens: int, parent_max_tokens: int, doc_title: str | None) -> None:
        self._max_tokens = max_tokens
        self._cap = parent_max_tokens
        self._doc_title = doc_title
        self._budgets: dict[tuple[str, ...], tuple[range, int]] = {}
        self._headings: dict[str, int] = {}
        self._prefixes: dict[tuple[str, ...], int] = {}
        self._texts: dict[str, int] = {}  # tokens of a block's text

    def groups(self, blocks: list[_Block]) -> list[list[_Block]]:
        """``blocks`` in runs, one run per parent, in document order."""
        if not blocks:
            return []
        if self._fits_whole(blocks):
            return [blocks]
        groups: list[list[_Block]] = []
        current: list[_Block] = []
        tokens = 0  # the parent ``current`` makes, measured (:meth:`_size`)

        def close() -> None:
            nonlocal current, tokens
            if current and groups and self._short(current):
                # Too short to stand alone (the empty headings ending a
                # document, after a new chapter closed the parent before):
                # it joins that parent when it fits, as a short section joins
                # the chunk before it (:func:`_join_short_sections`).
                joined = [*groups[-1], *current]
                if self._size(joined) <= self._cap:
                    groups[-1] = joined
                    current = []
            if current:
                groups.append(current)
            current, tokens = [], 0

        for block in blocks:
            if current and block.chapter != current[-1].chapter and 2 * tokens >= self._cap:
                close()  # a new chapter: parents follow chapters where they can
            if current:
                size = self._grown(current, tokens, block)
                if size <= self._cap:
                    current.append(block)
                    tokens = size
                    continue
            lead = current.pop() if current and self._leads(current[-1], block) else None
            if lead is not None:
                # The block opens the next parent with the line introducing
                # it — every parent its pieces fill, when it has to be cut.
                for piece, size in self._pieces([lead], block):
                    close()
                    current, tokens = [lead, piece], size
                continue
            carried = self._carried(current, block)
            if carried:
                # A short end of a section opens the next parent with the
                # block, as a short section joins the chunk after it.
                del current[-len(carried) :]
                close()
                (first, size), *rest = self._pieces(carried, block)
                current, tokens = [*carried, first], size
                pieces = [pair for piece, _ in rest for pair in self._pieces([], piece)]
            else:
                pieces = self._pieces([], block)
            for piece, size in pieces:
                if current and piece is not block:  # a piece may fit where the block did not
                    beside = self._grown(current, tokens, piece)
                    if beside <= self._cap:
                        current.append(piece)
                        tokens = beside
                        continue
                close()
                current, tokens = [piece], size
        close()
        return groups

    def parent(self, blocks: list[_Block], children: list[TextChunk]) -> ParentChunk:
        """The parent of the run ``blocks``, cut into ``children``."""
        path = children[0].heading_path if children else ()
        source = next(
            (i for i, block in enumerate(blocks) if path and self._shown(block) == path), None
        )
        return ParentChunk(
            body=self._render(blocks, source)[1], heading_path=path, children=tuple(children)
        )

    def _fits_whole(self, blocks: list[_Block]) -> bool:
        total = 0
        for block in blocks:
            total += self._text_tokens(block)
            if total > self._cap:
                return False
        return self._size(blocks) <= self._cap

    def _short(self, blocks: list[_Block]) -> bool:
        total = 0
        for block in blocks:
            total += self._text_tokens(block)
            if total >= SHORT_SECTION_TOKENS:
                return False
        return True

    def _carried(self, run: list[_Block], block: _Block) -> list[_Block]:
        """The end of ``run`` that opens the next parent with ``block`` rather
        than ending this one: the run's last blocks of one section, under
        :data:`SHORT_SECTION_TOKENS` together — ``block``'s section, or one a
        chunk may share with it (:func:`_may_join`) — as a short section joins
        the chunk after it (:func:`_join_short_sections`). None when the run
        is nothing else: a short run joins the parent before (``close``)."""
        if not run:
            return []
        trail = run[-1].heading_trail
        if trail != block.heading_trail and not _may_join(trail, block.heading_trail):
            return []
        start, total = len(run), 0
        while start and run[start - 1].heading_trail == trail:
            total += self._text_tokens(run[start - 1])
            if total >= SHORT_SECTION_TOKENS:
                return []
            start -= 1
        return run[start:] if start else []

    def _leads(self, lead: _Block, block: _Block) -> bool:
        """Whether ``lead`` is a line introducing ``block`` in its section
        (:func:`_is_lead_in`, judged against the section's chunk budget as
        :func:`_pack_blocks` judges it)."""
        trail = lead.heading_trail
        return trail == block.heading_trail and _is_lead_in(lead.text, self._budget(trail)[1])

    def _pieces(self, frame: list[_Block], block: _Block) -> list[tuple[_Block, int]]:
        """``block`` whole if it fits a parent after ``frame`` (nothing, or the
        line introducing it), else cut by the chunk rules (:func:`_prepare_block`)
        into pieces that each do — each with the size of the parent ``frame``
        and it make. Cut to the room the measured overrun leaves, so the
        title, breadcrumb, headings and joins the parent spends on top of the
        text are counted as they are."""
        size = self._grown(frame, self._size(frame) if frame else 0, block)
        over = size - self._cap
        if over <= 0:
            return [(block, size)]
        room = self._text_tokens(block) - over - _JOIN_TOKENS
        if room < self._cap // 4:
            # What surrounds the block fills the parent (a breadcrumb of
            # headings the length of a page): it stays whole, over the cap,
            # rather than cut into slivers.
            return [(block, size)]
        return [
            pair
            for text in _prepare_block(block, room)
            for pair in self._pieces(frame, replace(block, text=text))
        ]

    def _grown(self, blocks: Sequence[_Block], tokens: int, block: _Block) -> int:
        """The size of the parent ``blocks`` (of size ``tokens``) and ``block``
        after them make. Estimated well under the cap where the breadcrumb is
        known (:meth:`_estimate`); measured otherwise, so a run closes on
        measured sizes."""
        estimate = self._estimate(blocks, tokens, block)
        if estimate is not None and estimate <= self._cap - _ESTIMATE_SLACK:
            return estimate
        return self._size([*blocks, block])

    def _estimate(self, blocks: Sequence[_Block], tokens: int, block: _Block) -> int | None:
        """``tokens`` plus what ``block`` adds — the headings it enters, its
        text and the joins — which the joined text does not exceed; None when
        ``block`` may change the breadcrumb (the first one in the run that has
        one)."""
        trail = block.heading_trail
        if not blocks:
            kept = self._budget(trail)[0]
            entered = [heading for depth, heading in enumerate(trail) if depth not in kept]
            tokens = self._prefix_tokens(trail)
        elif (
            trail == blocks[-1].heading_trail
            or not self._shown(block)
            or any(self._shown(earlier) for earlier in blocks)
        ):
            entered = list(trail[_shared_depth(blocks[-1].heading_trail, trail) :])
        else:
            return None
        added = sum(self._heading_tokens(heading) + _JOIN_TOKENS for heading in entered)
        return tokens + added + self._text_tokens(block) + _JOIN_TOKENS

    def _prefix_tokens(self, trail: tuple[str, ...]) -> int:
        """Tokens of the title and breadcrumb in front of a parent opening
        with a block of ``trail``."""
        tokens = self._prefixes.get(trail)
        if tokens is None:
            kept = self._budget(trail)[0]
            prefix = chunk_retrieval_text(trail[kept.start : kept.stop], "", self._doc_title)
            tokens = self._prefixes[trail] = count_tokens(prefix)
        return tokens

    def _heading_tokens(self, heading: str) -> int:
        tokens = self._headings.get(heading)
        if tokens is None:
            tokens = self._headings[heading] = count_tokens(heading)
        return tokens

    def _text_tokens(self, block: _Block) -> int:
        tokens = self._texts.get(block.text)
        if tokens is None:
            tokens = self._texts[block.text] = count_tokens(block.text)
        return tokens

    def _size(self, blocks: Sequence[_Block]) -> int:
        """Tokens of the parent ``blocks`` make — title, breadcrumb, headings
        and body — with the larger of the breadcrumbs it may carry."""
        source = next((i for i, block in enumerate(blocks) if self._shown(block)), None)
        size = self._tokens(blocks, source)
        if source:  # a later section's breadcrumb: the parent may carry none
            size = max(size, self._tokens(blocks, None))
        return size

    def _tokens(self, blocks: Sequence[_Block], source: int | None) -> int:
        path, body = self._render(blocks, source)
        return count_tokens(chunk_retrieval_text(path, body, self._doc_title))

    def _render(self, blocks: Sequence[_Block], source: int | None) -> tuple[tuple[str, ...], str]:
        """Breadcrumb and body of a parent of ``blocks`` that carries the
        breadcrumb of ``blocks[source]`` (none when ``source`` is None). The
        body is the blocks' text with every heading the run enters written in
        front of the first text under it, save those the breadcrumb shows — the
        headings a child writes into its body too (:func:`_pack_blocks`,
        :func:`_join`)."""
        path: tuple[str, ...] = ()
        shown: set[tuple[str, ...]] = set()  # on the breadcrumb, not yet entered
        if source is not None:
            trail = blocks[source].heading_trail
            kept = self._budget(trail)[0]
            path = trail[kept.start : kept.stop]
            shown = {trail[: depth + 1] for depth in kept}
        parts: list[str] = []
        previous: tuple[str, ...] | None = None
        for block in blocks:
            trail = block.heading_trail
            if trail != previous:
                start = 0 if previous is None else _shared_depth(previous, trail)
                for depth in range(start, len(trail)):
                    if trail[: depth + 1] in shown:
                        shown.discard(trail[: depth + 1])
                    else:
                        parts.append(trail[depth])
                previous = trail
            parts.append(block.text)
        return path, "\n\n".join(parts)

    def _shown(self, block: _Block) -> tuple[str, ...]:
        """The breadcrumb the chunks of ``block``'s section carry."""
        kept = self._budget(block.heading_trail)[0]
        return block.heading_trail[kept.start : kept.stop]

    def _budget(self, trail: tuple[str, ...]) -> tuple[range, int]:
        budget = self._budgets.get(trail)
        if budget is None:
            budget = _section_budget(trail, self._doc_title, self._max_tokens)
            self._budgets[trail] = budget
        return budget


def _shared_depth(first: tuple[str, ...], second: tuple[str, ...]) -> int:
    """How many outer headings two heading trails share."""
    shared = 0
    while shared < min(len(first), len(second)) and first[shared] == second[shared]:
        shared += 1
    return shared


def _pack_blocks(
    blocks: list[_Block],
    *,
    max_tokens: int,
    overlap_tokens: int,
    semantic_breaks: frozenset[int],
    doc_title: str | None = None,
    written: set[tuple[str, ...]] | None = None,
) -> list[TextChunk]:
    """Pack blocks into chunks. Flush on a heading change, on a
    ``semantic_breaks`` index (a topic shift) once the chunk holds
    :data:`MIN_SEMANTIC_CHUNK_TOKENS`, and on the section's body budget
    (the cap less the title and breadcrumb, see :func:`_section_budget`).

    A heading the breadcrumb has no room for opens the body of the first
    chunk under it instead — the only place its text would otherwise have
    been, so it was lost (B-200 regression). A line that introduces the next
    block (:func:`_is_lead_in`, a question above its answer) never ends a
    chunk: it opens the next one with that block, and every further piece of
    a block cut after it repeats it. A section too short to stand on its own
    then joins a neighbouring chunk (:func:`_join_short_sections`).

    ``written`` holds the headings whose text the chunks of earlier parents
    of the document already hold (B-206 K-1) and takes those written here: a
    heading opens one chunk's body in the document, not one per parent."""
    sections = [_Section(())]
    trail: tuple[str, ...] = ()
    kept, budget = _section_budget(trail, doc_title, max_tokens)
    shown = trail[kept.start : kept.stop]
    # A piece cut from an oversized block leaves room for the overlap carried
    # in front of it, so the cap holds overlap included.
    reserve = overlap_tokens + _JOIN_TOKENS if overlap_tokens > 0 else 0
    buffer: list[str] = []
    buffer_tokens = 0
    if written is None:
        written = set()  # headings whose text went into a body

    def flush(*, carry_overlap: bool) -> None:
        nonlocal buffer, buffer_tokens
        if not buffer:
            return
        body = "\n\n".join(buffer)
        sections[-1].chunks.append(TextChunk(body=body, heading_path=shown))
        tail = _tail_text(body, overlap_tokens) if carry_overlap else ""
        buffer = [tail] if tail.strip() else []
        buffer_tokens = count_tokens(tail) if buffer else 0

    def add(block: _Block) -> None:
        nonlocal buffer, buffer_tokens
        lead = buffer[-1] if buffer and _is_lead_in(buffer[-1], budget) else ""
        if lead and buffer_tokens + _JOIN_TOKENS + count_tokens(block.text) > budget:
            # The block does not fit beside what is packed: it opens the next
            # chunk together with the line that introduces it.
            buffer.pop()
            buffer_tokens = count_tokens("\n\n".join(buffer)) if buffer else 0
            flush(carry_overlap=False)
            lead_tokens = count_tokens(lead) + _JOIN_TOKENS
            for piece in _prepare_block(block, budget - lead_tokens, reserve=reserve):
                flush(carry_overlap=False)
                buffer = [lead, piece]
                buffer_tokens = lead_tokens + count_tokens(piece)
            return
        for piece in _prepare_block(block, budget, reserve=reserve):
            piece_tokens = count_tokens(piece)
            if buffer and buffer_tokens + _JOIN_TOKENS + piece_tokens > budget:
                flush(carry_overlap=True)
                if buffer and buffer_tokens + _JOIN_TOKENS + piece_tokens > budget:
                    # No room for the carried overlap beside this piece — the
                    # cap bounds the whole text, so the chunk opens without it.
                    buffer, buffer_tokens = [], 0
            buffer_tokens += piece_tokens + (_JOIN_TOKENS if buffer else 0)
            buffer.append(piece)

    for index, block in enumerate(blocks):
        if block.heading_trail != trail:
            flush(carry_overlap=False)
            trail = block.heading_trail
            sections.append(_Section(trail))
            kept, budget = _section_budget(trail, doc_title, max_tokens)
            shown = trail[kept.start : kept.stop]
            for depth in range(len(trail)):
                if depth not in kept and trail[: depth + 1] not in written:
                    written.add(trail[: depth + 1])
                    add(_Block("content", trail[depth], trail))
        elif index in semantic_breaks and buffer_tokens >= MIN_SEMANTIC_CHUNK_TOKENS:
            # Topic shift inside a section — a clean boundary, no overlap. A
            # shorter fragment stays and opens the next chunk instead.
            flush(carry_overlap=False)
        add(block)
    flush(carry_overlap=False)
    return _join_short_sections(sections, max_tokens=max_tokens, doc_title=doc_title)


def _join_short_sections(
    sections: list[_Section], *, max_tokens: int, doc_title: str | None
) -> list[TextChunk]:
    """Chunks of every section in order, with each section shorter than
    :data:`SHORT_SECTION_TOKENS` joined to a neighbour (B-206 K-3): to the
    first chunk of the next section — a join still that short takes the next
    one too — else to the chunk before it. Only a sibling or a subsection
    joins (:func:`_may_join`), and a join never passes the cap; otherwise the
    short section stays a chunk of its own."""

    def fits(unit: _Unit) -> bool:
        text = chunk_retrieval_text(unit.chunk.heading_path, unit.chunk.body, doc_title)
        return count_tokens(text) <= max_tokens

    def keep(short: _Unit) -> None:
        if out and _may_join(out[-1].trail, short.trail):
            joined = _join(out[-1], short)
            if fits(joined):
                out[-1] = joined
                return
        out.append(short)

    out: list[_Unit] = []
    pending: _Unit | None = None
    for section in sections:
        for chunk in section.chunks:
            unit = _Unit(chunk, section.trail, whole=len(section.chunks) == 1)
            if pending is not None:
                joined = _join(pending, unit) if _may_join(pending.trail, unit.trail) else None
                if joined is not None and fits(joined):
                    unit = joined
                else:
                    keep(pending)
                pending = None
            size = count_tokens(unit.chunk.body)
            if unit.whole and size < SHORT_SECTION_TOKENS:
                pending = unit
            else:
                out.append(unit)
    if pending is not None:
        keep(pending)
    return [unit.chunk for unit in out]


def _may_join(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Whether the section at ``second`` may share a chunk under the
    breadcrumb of the one at ``first`` before it: a sibling (same parent) or
    a section inside it. Text after the preamble (no heading) always may."""
    return second[:-1] == first[:-1] or second[: len(first)] == first


def _join(first: _Unit, second: _Unit) -> _Unit:
    """``second``'s body appended to ``first``'s chunk, under ``first``'s
    breadcrumb (``second``'s when ``first`` has none). The headings of
    ``second`` that breadcrumb does not carry open its part of the body, so
    no heading's text is lost."""
    if first.chunk.heading_path:
        path, trail = first.chunk.heading_path, first.trail
        shared = 0
        while shared < min(len(first.trail), len(second.trail)) and (
            first.trail[shared] == second.trail[shared]
        ):
            shared += 1
        headings = second.trail[shared:]
    else:
        path, trail, headings = second.chunk.heading_path, second.trail, ()
    body = "\n\n".join([first.chunk.body, *headings, second.chunk.body])
    return _Unit(TextChunk(body=body, heading_path=path), trail, whole=first.whole and second.whole)


def _section_budget(
    trail: tuple[str, ...], doc_title: str | None, max_tokens: int
) -> tuple[range, int]:
    """The headings of ``trail`` a section's chunks carry as their breadcrumb
    (a range of its depths) and the body budget left beside it. The cap
    bounds the whole embedded text — title, breadcrumb and body, as Onyx sizes
    its chunks (B-200) — so no embedding input outgrows the chunk size. A
    breadcrumb over a third of the cap loses its outermost headings first (a
    quarter of 400 cost continuation chunks of deep sections levels they kept
    at 512 — B-206 K-3 dev set);
    when even the innermost one alone is too long (a slide title read as a
    heading), the outer headings that fit stay instead. The body keeps at
    least half the cap."""

    def prefix_tokens(start: int, stop: int) -> int:
        return count_tokens(chunk_retrieval_text(trail[start:stop], "", doc_title))

    limit = max_tokens // 3
    start, stop = 0, len(trail)
    while start < stop and prefix_tokens(start, stop) > limit:
        start += 1
    if start == stop:
        start = 0
        while stop and prefix_tokens(0, stop) > limit:
            stop -= 1
    return range(start, stop), max(max_tokens - prefix_tokens(start, stop), max_tokens // 2)


async def _semantic_breaks(
    blocks: list[_Block], embedder: Embedder, tenant_id: UUID, memo: BreakMemo | None = None
) -> frozenset[int]:
    """Block indices that start a new semantic segment — adjacent
    same-section blocks whose embeddings exceed the distance threshold.
    A heading change is already a flush point, so it is skipped here.
    Blocks are embedded :data:`SEMANTIC_EMBED_WINDOW` at a time and only the
    previous block's vector outlives its window. With a ``memo``, a pair it
    already knows is not embedded again: only the blocks of the other pairs are."""
    pairs = [
        index
        for index in range(1, len(blocks))
        if blocks[index].heading_trail == blocks[index - 1].heading_trail
    ]
    breaks: set[int] = set()
    keys: dict[int, str] = {}
    if memo is not None:
        keys = {index: memo.key(blocks[index - 1].text, blocks[index].text) for index in pairs}
        for index in pairs:
            known = memo.known.get(keys[index])
            if known is not None:
                memo.decided[keys[index]] = known
                if known:
                    breaks.add(index)
        pairs = [index for index in pairs if keys[index] not in memo.decided]
        needed = sorted({side for index in pairs for side in (index - 1, index)})
    else:
        needed = list(range(len(blocks)))
    open_pairs = set(pairs)
    previous: tuple[int, Sequence[float]] | None = None
    for start in range(0, len(needed), SEMANTIC_EMBED_WINDOW):
        window = needed[start : start + SEMANTIC_EMBED_WINDOW]
        vectors = await embedder.embed(
            [blocks[index].text for index in window], tenant_id=tenant_id
        )
        for index, vector in zip(window, vectors, strict=True):
            if index in open_pairs and previous is not None and previous[0] == index - 1:
                shift = _cosine_distance(previous[1], vector) >= _SEMANTIC_BREAK_THRESHOLD
                if shift:
                    breaks.add(index)
                if memo is not None:
                    memo.decided[keys[index]] = shift
            previous = (index, vector)
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
    tagged with the heading breadcrumb of the section it sits in.

    No source text is dropped (B-199): lines markdown-it consumes without a
    block token (a link reference definition) are kept as content, and a
    heading no content ever sits under is kept as content too — otherwise its
    text would reach no chunk. Only ATX ``#`` headings name a section: a line
    above ``---`` / ``===`` is a paragraph, as Dify and FastGPT read it."""
    tokens = MarkdownIt().enable("table").disable("lheading").parse(markdown)
    lines = _LINE_BREAK.split(markdown)
    blocks: list[_Block] = []
    trail_stack: list[_Heading] = []
    covered = 0  # lines before this index are already in a block or a heading
    chapter = 0  # level-1 / level-2 headings opened so far

    def emit(kind: Literal["content", "table"], text: str) -> None:
        for heading in trail_stack:
            heading.used = True
        blocks.append(_Block(kind, text, tuple(h.text for h in trail_stack), chapter))

    def keep(upto: int) -> None:
        text = "\n".join(lines[covered:upto]).strip()
        if text:
            emit("content", text)

    def close_sections(level: int) -> None:
        while trail_stack and trail_stack[-1].level >= level:
            heading = trail_stack.pop()
            if not heading.used:
                emit("content", heading.text)

    for index, token in enumerate(tokens):
        if token.level != 0 or token.map is None:
            continue
        start, end = token.map
        if start > covered:
            keep(start)
        covered = max(covered, end)
        if token.type == "heading_open":
            text = tokens[index + 1].content.strip() if index + 1 < len(tokens) else ""
            if len(text) <= _MAX_HEADING_CHARS:
                level = int(token.tag[1:])
                close_sections(level)
                if level <= _CHAPTER_LEVEL:
                    chapter += 1
                trail_stack.append(_Heading(level, text))
                continue
        block_text = "\n".join(lines[start:end]).strip()
        if not block_text:
            continue
        if token.type == "table_open":
            emit("table", block_text)
        elif (
            token.type in _CONTENT_OPENERS
            or token.type in _CONTENT_STANDALONE
            or token.type == "heading_open"
        ):
            emit("content", block_text)
    keep(len(lines))
    close_sections(0)
    return blocks


def _parse_plain_blocks(text: str) -> list[_Block]:
    """Plain text (``.txt``) as paragraphs between blank lines, none of them
    in a section: nothing in a line is Markdown structure (B-199) — the way
    Dify, RAGFlow and Onyx read a text file."""
    blocks: list[_Block] = []
    paragraph: list[str] = []
    for line in [*_LINE_BREAK.split(text), ""]:
        if line.strip():
            paragraph.append(line)
            continue
        body = "\n".join(paragraph).strip()
        if body:
            blocks.append(_Block("content", body, ()))
        paragraph = []
    return blocks


def _blocks(text: str, *, plain_text: bool) -> list[_Block]:
    return _parse_plain_blocks(text) if plain_text else _parse_blocks(text)


def _presplit_blocks(blocks: list[_Block], max_tokens: int) -> list[_Block]:
    """Cut every block over ``max_tokens`` into cap-sized pieces that keep its
    kind and heading trail; blocks within the cap pass through untouched."""
    return [
        replace(block, text=piece)
        for block in blocks
        for piece in _prepare_block(block, max_tokens)
    ]


def _prepare_block(block: _Block, max_tokens: int, *, reserve: int = 0) -> list[str]:
    """Return ``block`` as one or more pieces, each within ``max_tokens``. A
    block that has to be cut is cut ``reserve`` tokens shorter (but to no less
    than a quarter of the cap), leaving room for what is packed in front of
    each piece."""
    if count_tokens(block.text) <= max_tokens:
        return [block.text]
    size = max(max_tokens - reserve, max_tokens // 4)
    if block.kind == "table":
        return _split_table(block.text, size)
    return _split_content(block.text, size)


def _split_content(text: str, max_tokens: int) -> list[str]:
    """Cut oversized prose where a cut does least harm (B-206 K-3): first in
    front of a line that opens a new part and after a page break
    (:data:`_CUT_LINE`), then — in a block of lines, a conversation or a list
    of short lines — between lines, else on sentence boundaries."""
    starts = {m.start() for m in _CUT_LINE.finditer(text)}
    starts |= {m.end() for m in re.finditer("\f", text)}
    bounds = [0, *sorted(starts - {0, len(text)}), len(text)]
    parts: list[str] = []
    for part in (text[a:b] for a, b in pairwise(bounds)):
        if parts and _CUT_LINE.match(parts[-1]) and not _LINE_BREAK.search(parts[-1].rstrip()):
            # A cut line alone (a chapter line over its first article) opens
            # the part after it, not the end of the piece before.
            parts[-1] += part
        else:
            parts.append(part)
    return _pack_parts(parts, max_tokens, _split_part)


def _split_part(text: str, max_tokens: int) -> list[str]:
    """One oversized part with no cut line in it: between lines if it is a
    block of lines (:func:`_is_line_block`), else on sentence boundaries with
    the sentence before each cut repeated (:func:`_cut_with_overlap`)."""
    if _is_line_block(text):
        return _pack_parts(_LINE_WITH_END.findall(text), max_tokens, _cut_with_overlap)
    return _cut_with_overlap(text, max_tokens)


def _cut_with_overlap(text: str, max_tokens: int) -> list[str]:
    """``text`` cut on sentence boundaries (:func:`_hard_split`), each piece
    after the first opening with the last whole sentences of the one before
    it — at most :data:`_CUT_OVERLAP_PERCENT` of the cap; none when even the
    last sentence is longer — so a passage the cut runs through is still
    whole in one piece."""
    overlap = max_tokens * _CUT_OVERLAP_PERCENT // 100
    pieces = _hard_split(text, max_tokens - overlap - 2 * _JOIN_TOKENS)
    out = pieces[:1]
    for before, piece in pairwise(pieces):
        tail = _sentence_tail(before, overlap)
        if not tail:
            out.append(piece)
        else:
            out.append(f"{tail}{piece}" if _cjk_end(tail) else f"{tail} {piece}")
    return out


def _sentence_tail(text: str, max_tokens: int) -> str:
    """The last whole sentences of ``text`` that fit in ``max_tokens``."""
    taken: list[str] = []
    tokens = 0
    for unit in reversed([u for u in _SENTENCE_BOUNDARY.split(text) if u.strip()]):
        tokens += count_tokens(unit)
        if tokens > max_tokens:
            break
        taken.append(unit)
    return "".join(reversed(taken)).strip()


def _cjk_end(text: str) -> bool:
    """Whether ``text`` ends in a CJK character or mark (no space follows)."""
    return bool(text) and ord(text[-1]) >= 0x2E80


def _is_lead_in(piece: str, budget: int) -> bool:
    """Whether ``piece`` is one line that introduces what follows it: a
    question, a line ending in a colon, a bold line, a numbered or chapter
    line — short (a quarter of the ``budget`` at most), not a table row. A
    chunk ending on one leaves the text it introduces without it (B-206 K-3
    dev set: an FAQ question at the end of a chunk, its answer in the next)."""
    text = piece.strip()
    if not text or text.startswith("|") or _LINE_BREAK.search(text):
        return False
    if count_tokens(text) > budget // 4:
        return False
    bold = len(text) > 4 and text.startswith("**") and text.endswith("**")
    return bold or bool(_LEAD_IN_END.search(text.strip("*_ \t"))) or bool(_CUT_LINE.match(text))


def _is_line_block(text: str) -> bool:
    """A conversation (most lines open with a speaker label) or a list of
    short lines — one utterance or entry per line, not sentences of prose."""
    lines = [line.strip() for line in _LINE_BREAK.split(text) if line.strip()]
    if len(lines) < _LINE_BLOCK_MIN_LINES:
        return False
    if 2 * sum(bool(_SPEAKER.match(line)) for line in lines) > len(lines):
        return True
    return statistics.median(len(line) for line in lines) < _SHORT_LINE_CHARS


def _pack_parts(
    parts: list[str], max_tokens: int, cut: Callable[[str, int], list[str]]
) -> list[str]:
    """Consecutive ``parts`` (pieces of one text, in order) packed into pieces
    of at most ``max_tokens``, each the parts' text exactly; a part over the
    cap on its own goes to ``cut``. A line that introduces the next part
    (:func:`_is_lead_in`) opens the next piece rather than ending one."""
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def close() -> None:
        nonlocal current_tokens
        piece = "".join(current).strip()
        if piece:
            pieces.append(piece)
        current.clear()
        current_tokens = 0

    for part in parts:
        part_tokens = count_tokens(part)
        if part_tokens > max_tokens:
            close()
            if part.strip():
                pieces.extend(cut(part.strip(), max_tokens))
            continue
        if current and current_tokens + part_tokens > max_tokens:
            lead = (
                current.pop() if len(current) > 1 and _is_lead_in(current[-1], max_tokens) else ""
            )
            close()
            if lead and count_tokens(lead) + part_tokens <= max_tokens:
                current.append(lead)
                current_tokens = count_tokens(lead)
            elif lead:
                pieces.append(lead.strip())
        current.append(part)
        current_tokens += part_tokens
    close()
    return pieces


def _split_table(table: str, max_tokens: int) -> list[str]:
    """Split an oversized Markdown table into parts within the cap. A row
    with another column count than the table's first row starts a new table
    (B-206 K-3) — two tables run together keep their own headers."""
    lines = _LINE_BREAK.split(table)
    tables: list[list[str]] = []
    for line in lines:
        if tables and _cell_count(line) == _cell_count(tables[-1][0]):
            tables[-1].append(line)
        else:
            tables.append([line])
    return [part for rows in tables for part in _split_one_table(rows, max_tokens)]


def _cell_count(row: str) -> int:
    return len(_CELL_SPLIT.split(row.strip().strip("|")))


def _split_one_table(lines: list[str], max_tokens: int) -> list[str]:
    """Split one table by rows, repeating the header + separator rows in every
    part so each part is a valid table. A row that alone outruns the cap (a
    cell holding a whole exported chat record, a long FAQ answer) is cut into
    rows of its own (:func:`_split_long_row`), each under the header; a header
    that leaves under a quarter of the cap is cut along with the rest of the
    table. Rows with no header row above them are packed as they are."""
    table = "\n".join(lines)
    if count_tokens(table) <= max_tokens:
        return [table]
    if len(lines) < 2 or not _TABLE_SEPARATOR_ROW.match(lines[1]):
        return _pack_parts([f"{line}\n" for line in lines], max_tokens, _hard_split)
    header = "\n".join(lines[:2])
    room = max_tokens - count_tokens(header) - _JOIN_TOKENS
    if room < max_tokens // 4:
        return _hard_split(table, max_tokens)
    parts: list[str] = []
    current: list[str] = []
    for row in lines[2:]:
        if count_tokens(row) > room:
            if current:
                parts.append("\n".join([header, *current]))
                current = []
            parts.extend(f"{header}\n{piece}" for piece in _split_long_row(row, room))
            continue
        candidate = "\n".join([header, *current, row])
        if current and count_tokens(candidate) > max_tokens:
            parts.append("\n".join([header, *current]))
            current = [row]
        else:
            current.append(row)
    if current:
        parts.append("\n".join([header, *current]))
    return parts


def _split_long_row(row: str, max_tokens: int) -> list[str]:
    """A table row over ``max_tokens`` as several rows: its longest cell cut
    (:func:`_cut_with_overlap`), every piece in a row that repeats the cells
    in front of it — the category / question an FAQ sheet keeps there — the
    cells after it on the first piece only (B-206 K-3; Docling, Unstructured,
    FastGPT and Onyx all carry the header alone, so a continuation never says
    which question it answers). Cells too long to repeat: cut like prose."""
    cells = [cell.strip() for cell in _CELL_SPLIT.split(row.strip().strip("|"))]
    longest = max(range(len(cells)), key=lambda index: count_tokens(cells[index]))

    def line(values: Sequence[str]) -> str:
        return "| " + " | ".join(values) + " |"

    lead, after = cells[:longest], cells[longest + 1 :]
    frame = count_tokens(line([*lead, "", *after]))
    if len(cells) < 2 or frame > max_tokens // 2:
        return _hard_split(row, max_tokens)
    pieces = _cut_with_overlap(cells[longest], max_tokens - frame - 2 * _JOIN_TOKENS)
    blank = [""] * len(after)
    return [
        line([*lead, piece, *(after if index == 0 else blank)])
        for index, piece in enumerate(pieces)
    ]


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
    """Last-resort split of an unbreakable run on token windows. Each cut
    moves back to the last word boundary inside its window, so a word is torn
    only when it alone outruns the window (a torn "Triage" matched nothing,
    B-199); cuts are character offsets, so a multi-byte character is never
    torn in two (a plain ``decode`` of a token slice yields U+FFFD for CJK)."""
    token_ids = _encode(text)
    decoded, offsets = _encoding.decode_with_offsets(token_ids)
    count = len(token_ids)
    pieces: list[str] = []
    start = 0
    while start < count:
        end = min(start + max_tokens, count)
        if end < count:
            end = next(
                (t for t in range(end, start, -1) if _between_words(decoded, offsets[t])), end
            )
        piece = decoded[offsets[start] : offsets[end] if end < count else len(decoded)]
        if piece:
            pieces.append(piece)
        start = end
    return pieces


def _between_words(text: str, at: int) -> bool:
    """Whether a cut at character ``at`` of ``text`` falls on whitespace."""
    return 0 < at < len(text) and (text[at].isspace() or text[at - 1].isspace())


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
