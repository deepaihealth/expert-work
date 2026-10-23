"""Bounds on what document ingestion holds in the server process: the
per-document cap on parsed text, the server's budget for it, and the budget
for original upload bytes.

Everything after parsing runs in the server process: the parsed text is
chunked, every chunk gets a 1,024-float vector, and the rows are written in one
transaction. A compressed upload can unpack to far more text than its size
suggests (a 0.5 MB Word file held 110 MB of text and took the server to
5.5 GB), and the server's memory for a document follows its **chunks**: about
11 KiB a chunk plus 20 MiB (B-214, 10-23 — vectors kept compact, chunks
embedded a window at a time and built as the store reads them; ~64 KiB a
chunk before), whatever their length.

Bounds on the characters (``knowledge_document_max_chars``) and the chunks
(``knowledge_document_max_chunks``):

* **the cap** — a document whose parsed text is longer is refused with
  ``error_code=text_too_large`` and a message that asks for it to be split.
  The parse worker measures its own output and refuses there, so the text
  never crosses into the server (:func:`parse_within_cap`,
  :func:`analyse_pdf_within_cap`, :func:`parse_office_figures_within_cap`).
  The cap is on the text that reaches chunking — figure descriptions spliced
  in after parsing (B-209 PR-2) count — so the server checks the final text
  again (:func:`check_parsed_size`, also for pre-parsed Markdown); a worker's
  output over the cap can only grow, so refusing it early never refuses a
  document the final check would have let through. The refusal is deterministic, so it is never
  retried. Scanned-page recognition stops once what it read fills the room
  the cap leaves (:func:`recognized_text_limit`): the remaining pages are
  recorded as ``document_text_limit`` and the document still ingests.
* **the chunk cap** (B-214) — a heading breaks a chunk however short it is,
  so 2.5M characters of short headed entries are ~58,000 chunks. A document
  over the cap is refused with ``error_code=too_many_chunks``: on its
  structural chunks before anything is embedded, on its semantic chunks
  before any chunk is (:func:`ensure_chunk_count`). Also deterministic.
* **the budget** — the server embeds / stores at most a character cap's worth
  at once, over every document in flight (the upload path and the recovery
  worker alike), a document weighing its characters or its chunks at the two
  caps' ratio, whichever is more (:func:`admission_weight`). A document waits
  in arrival order until it fits (:func:`admitted`), so concurrent ingests
  cannot add up past what one document at the caps may use. Chunking happens
  before (it holds the text and one window of block vectors), so the weight
  is the real chunk count.

And **the raw-bytes budget** (``knowledge_ingest_raw_bytes_budget``): a claim
holds its document's original bytes for the whole ingest — parsing and every
batch of scanned-page renders re-send them to the parse worker — so each claim
(upload path and recovery worker) reserves the largest upload before it reads
them, keeps their real size, and waits in arrival order while they would not
fit (:func:`reserve_raw_bytes`).

Characters first: the length of the parsed text is known in the worker before
anything is sent back, costs nothing to measure, means the same for every
format, and is what a user can act on. Chunks are counted in the server, but
before anything is embedded — what memory actually follows (B-214).
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from control_plane.knowledge.parsing import DocumentParseError, parse_document
from control_plane.knowledge.recognition.pages import PdfAnalysis, analyse_pdf
from expert_work.protocol import IngestErrorCode, PageDecision

if TYPE_CHECKING:
    from control_plane.knowledge.recognition.figure_extract import OfficeParse

#: Default of ``Settings.knowledge_document_max_chars`` (kept equal by a test):
#: the limit in effect until :func:`configure_parsed_size` sets the deployment's.
DEFAULT_MAX_PARSED_CHARS = 2_500_000

_max_chars = DEFAULT_MAX_PARSED_CHARS

#: Default of ``Settings.knowledge_document_max_chunks`` (B-214): the server's
#: memory for an ingest follows its chunk count — about 11 KiB a chunk once
#: vectors are compact (``chunk_rows``), 20 MiB fixed — so 40,000 chunks stay
#: near 460 MiB. Characters alone cannot bound that: a document of short
#: headed entries is ~43 characters a chunk, and a heading breaks a chunk
#: however short it is.
DEFAULT_MAX_CHUNKS = 40_000

_max_chunks = DEFAULT_MAX_CHUNKS

#: The numbers in a refusal's message, for readers that render it in their
#: own words (the admin console does). Changing the message means changing
#: this pattern — and the console's copy of it (``documentError.ts``).
TOO_LARGE_PATTERN = re.compile(r"([\d,]+) characters, over the per-document limit of ([\d,]+)")


class DocumentTooLargeError(DocumentParseError):
    """The parsed text of a document is over the per-document cap."""

    def __init__(self, chars: int, limit: int) -> None:
        super().__init__(too_large_message(chars, limit), IngestErrorCode.TEXT_TOO_LARGE)
        self.chars = chars
        self.limit = limit

    def __reduce__(self) -> tuple[type[DocumentTooLargeError], tuple[int, int]]:  # type: ignore[override]
        # Raised in the parse worker: travels back by pickle.
        return (type(self), (self.chars, self.limit))


#: The numbers in a chunk-count refusal (``documentError.ts`` keeps a copy).
TOO_MANY_CHUNKS_PATTERN = re.compile(r"([\d,]+) chunks, over the per-document limit of ([\d,]+)")


class DocumentTooManyChunksError(DocumentParseError):
    """The document splits into more chunks than one document may hold."""

    def __init__(self, chunks: int, limit: int) -> None:
        super().__init__(too_many_chunks_message(chunks, limit), IngestErrorCode.TOO_MANY_CHUNKS)
        self.chunks = chunks
        self.limit = limit

    def __reduce__(self) -> tuple[type[DocumentTooManyChunksError], tuple[int, int]]:  # type: ignore[override]
        return (type(self), (self.chunks, self.limit))


def too_many_chunks_message(chunks: int, limit: int) -> str:
    """The chunk-count refusal recorded as the document's ``error``."""
    return (
        f"too many chunks: {chunks:,} chunks, over the per-document limit of {limit:,}; "
        "split the document into smaller files and upload them separately"
    )


def ensure_chunk_count(chunks: int) -> None:
    """:raises DocumentTooManyChunksError: ``chunks`` is over the cap in effect."""
    if chunks > _max_chunks:
        raise DocumentTooManyChunksError(chunks, _max_chunks)


def too_large_message(chars: int, limit: int) -> str:
    """The refusal recorded as the document's ``error`` (numbers first, so
    a truncated message still carries them)."""
    return (
        f"text too large: {chars:,} characters, over the per-document limit of {limit:,}; "
        "split the document into smaller files and upload them separately"
    )


def configure_parsed_size(*, max_chars: int, max_chunks: int = DEFAULT_MAX_CHUNKS) -> None:
    """Set the caps / budget (lifespan wiring). Call before the first ingest."""
    global _max_chars, _max_chunks
    _max_chars = max(1, max_chars)
    _max_chunks = max(1, max_chunks)
    _budgets.pop("text", None)


def max_parsed_chars() -> int:
    """The per-document cap in effect."""
    return _max_chars


def ensure_within_cap(chars: int, limit: int) -> None:
    """:raises DocumentTooLargeError: ``chars`` is over ``limit``."""
    if chars > limit:
        raise DocumentTooLargeError(chars, limit)


def check_parsed_size(text: str) -> None:
    """Server-side check of text that did not come from a parse worker."""
    ensure_within_cap(len(text), _max_chars)


def parse_within_cap(filename: str, raw: bytes, limit: int) -> str:
    """:func:`parse_document` that refuses an over-cap result in the parse
    worker, before it is pickled back to the server."""
    text = parse_document(filename, raw)
    ensure_within_cap(len(text), limit)
    return text


def analyse_pdf_within_cap(filename: str, raw: bytes, limit: int) -> PdfAnalysis:
    """:func:`analyse_pdf` with the same refusal, on the text it returns: the
    whole Markdown, or the text pages' (before any page is recognized)."""
    analysis = analyse_pdf(filename, raw)
    if analysis.markdown is not None:
        chars = len(analysis.markdown)
    else:
        chars = sum(len(page.text) for page in analysis.pages)
    ensure_within_cap(chars, limit)
    return analysis


def parse_office_figures_within_cap(raw: bytes, ext: str, limit: int) -> OfficeParse:
    """B-209 PR-2's Word / slide parse with figures pulled out, with the same
    refusal on its Markdown (figure placeholders included; the descriptions
    spliced in later only add to it). Imported lazily: most parse-worker
    calls never touch the figure code."""
    from control_plane.knowledge.recognition.figure_extract import parse_office_figures

    parsed = parse_office_figures(raw, ext)
    ensure_within_cap(len(parsed.markdown), limit)
    return parsed


def recognized_text_limit(analysis: PdfAnalysis, ceiling: int) -> int:
    """How much recognized text a PDF's scanned pages may add: ``ceiling``, or
    less — what the cap leaves beside the text pages' own text and the blank
    line joining every page — so a document is never refused over text that
    recognition was paid to read. Recognition stops once it is reached."""
    kept = sum(len(p.text.strip()) for p in analysis.pages if p.decision is not PageDecision.OCR)
    return max(0, min(ceiling, _max_chars - kept - 2 * len(analysis.pages)))


@dataclass
class _Budget:
    """What is held now, and the waiters in arrival order."""

    capacity: int
    used: int = 0
    waiters: deque[tuple[int, asyncio.Future[None]]] = field(default_factory=deque)

    def wake(self) -> None:
        while self.waiters:
            weight, future = self.waiters[0]
            if future.done():  # cancelled while waiting
                self.waiters.popleft()
                continue
            if self.used + weight > self.capacity:
                return  # first come, first served: nobody overtakes the head
            self.waiters.popleft()
            self.used += weight
            future.set_result(None)


class Hold:
    """One document's share of a budget. :meth:`shrink` gives back what it
    turned out not to need; :meth:`release` gives back the rest (idempotent)."""

    def __init__(self, budget: _Budget, weight: int) -> None:
        self._budget = budget
        self._weight = weight

    def shrink(self, weight: int) -> None:
        kept = min(max(weight, 0), self._weight)
        self._budget.used -= self._weight - kept
        self._weight = kept
        self._budget.wake()

    def release(self) -> None:
        self.shrink(0)


async def _acquire(budget: _Budget, weight: int) -> Hold:
    """Wait (in arrival order) until ``weight`` fits, then hold it."""
    weight = min(max(weight, 0), budget.capacity)
    if budget.waiters or budget.used + weight > budget.capacity:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        budget.waiters.append((weight, future))
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                budget.used -= weight  # admitted, then cancelled before running
            else:
                future.cancel()
            budget.wake()
            raise
    else:
        budget.used += weight
    return Hold(budget, weight)


#: name → ``(loop, budget)`` — futures belong to one loop, so a budget is
#: recreated when the running loop changes (tests).
_budgets: dict[str, tuple[asyncio.AbstractEventLoop, _Budget]] = {}


def _get_budget(name: str, capacity: int) -> _Budget:
    loop = asyncio.get_running_loop()
    held = _budgets.get(name)
    if held is None or held[0] is not loop:
        held = (loop, _Budget(capacity))
        _budgets[name] = held
    return held[1]


def admission_weight(chars: int, chunks: int) -> int:
    """What a document holds of the budget: its characters, or its chunks at
    the cap's ratio (a document at the chunk cap weighs a whole budget) when
    those cost more — memory follows chunks once a document is chunked."""
    return max(chars, math.ceil(chunks * _max_chars / _max_chunks))


@asynccontextmanager
async def admitted(chars: int, chunks: int = 0) -> AsyncIterator[None]:
    """Hold a document's share of the server's ingest budget
    (:func:`admission_weight`) while it is embedded and stored; wait (in
    arrival order) until it fits."""
    hold = await _acquire(_get_budget("text", _max_chars), admission_weight(chars, chunks))
    try:
        yield
    finally:
        hold.release()


# ---------------------------------------------------------------------------
# Raw upload bytes
# ---------------------------------------------------------------------------

#: Default of ``Settings.knowledge_ingest_raw_bytes_budget``: two of the
#: largest uploads (``knowledge_document_max_bytes``) at once per process.
DEFAULT_RAW_BYTES_BUDGET = 100 * 1024 * 1024
#: What a claim reserves before it reads a document's bytes — the largest
#: upload, since the size is only known once they are read.
DEFAULT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

_raw_bytes_budget = DEFAULT_RAW_BYTES_BUDGET
_upload_max_bytes = DEFAULT_UPLOAD_MAX_BYTES


def configure_raw_bytes(*, budget: int, upload_max_bytes: int) -> None:
    """Set the raw-bytes budget and the reservation (lifespan wiring)."""
    global _raw_bytes_budget, _upload_max_bytes
    _raw_bytes_budget = max(1, budget)
    _upload_max_bytes = max(1, upload_max_bytes)
    _budgets.pop("raw", None)


async def reserve_raw_bytes() -> Hold:
    """Reserve room for one document's original bytes before a claim reads
    them; :meth:`Hold.shrink` to their real size once read, release when the
    ingest is over. Every claim (upload path and recovery worker) holds its
    bytes for the whole ingest — parsing and page rendering re-send them to
    the parse worker — so the budget bounds what all of them hold at once."""
    return await _acquire(_get_budget("raw", _raw_bytes_budget), _upload_max_bytes)


@asynccontextmanager
async def raw_bytes_held() -> AsyncIterator[Hold]:
    """:func:`reserve_raw_bytes` as a context manager (released on exit)."""
    hold = await reserve_raw_bytes()
    try:
        yield hold
    finally:
        hold.release()
