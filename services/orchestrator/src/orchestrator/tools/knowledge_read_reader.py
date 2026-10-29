"""How ``knowledge_read`` reads one document (B-206 K-2).

:class:`DocumentReader` takes the document's readable passages and lays out what
one call asked for — a page, the passages around one, a section, the passages
that contain given words — as the tool's result: the document's details, the
passages (each labelled ``[D12#7-9]``), a notice of platform facts (page n of N,
what to pass next) and ``meta`` recording what was read.

Sizes are tokens (:class:`PassageTokens`), counted off the event loop, as is
find's normalising of every passage. A page fills to :data:`PAGE_SHARE` of
``max_tokens`` and always holds one whole passage; find keeps its hits whole and
adds a neighbour only when it fits; a passage longer than :data:`MAX_MAX_TOKENS`
(only a hand edit makes one) is cut and marked. Tenant text — file names,
titles, base names, metadata, headings, bodies — goes in the content only;
notices and errors carry ``D`` references, numbers and fixed text
(:attr:`OpenDocument.spoken`).
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from expert_work.protocol import DocumentStatus, KnowledgeBase, KnowledgeDocument
from expert_work.runtime.tokens import TiktokenEstimator, TokenEstimator
from orchestrator.tools.error_classifier import GuidedToolError
from orchestrator.tools.knowledge_read_view import (
    ReadPassage,
    Role,
    around_window,
    find_matches,
    fit_greedily,
    haystack,
    in_section,
    label,
    neighbours,
    normalize,
    outline,
    pack_pages,
    section_paths,
    squeeze,
    wanted_path,
    word_counts,
)
from orchestrator.tools.knowledge_results import RESULT_SEPARATOR, TRUNCATED_MARK
from orchestrator.tools.registry import ToolResult

#: The largest page; also the most one passage shows: a longer one is cut and
#: marked.
MAX_MAX_TOKENS = 16_000
#: A page fills to this share of ``max_tokens`` (WeKnora cuts its pages at 80%
#: of the output budget); the document's details and the marks take the rest.
PAGE_SHARE = 0.8
MAX_FIND_MATCHES = 20
MAX_FIND_WORDS = 10
MAX_ARGUMENT_CHARS = 200
MAX_LISTED_HEADINGS = 20
_DETAIL_MAX_CHARS = 120
#: ``metadata`` keys shown in the details (a platform convention, design K-4).
_METADATA_DETAILS = (
    ("version", "version"),
    ("effective_date", "effective date"),
    ("source", "source"),
)
_MATCH_MARK = " · match"

_PASSAGE_ADVICE = "Read the document by page, or search again for a current reference."
_NO_SECTION_NOTICE = (
    "[knowledge_read: nothing was read; pass a heading listed in the result as section, or read "
    "the document by page or with find.]"
)
_NO_MATCH_NOTICE = (
    "[knowledge_read: find needs every word in the same passage, in any order, ignoring case, "
    "width and spacing; fewer words, or other words in the document's own terms, can find what "
    "this missed, and knowledge_search searches every document.]"
)
_REINGEST_NOTICE = (
    "[knowledge_read: a newer version of this document is being ingested; this is the version "
    "in use.]"
)
#: A newer upload whose ingest failed or was cancelled (``has_pending``).
_NOT_INGESTED_NOTICE = (
    "[knowledge_read: a newer version of this document was uploaded but not ingested (its "
    "ingest failed or was cancelled); this is the version in use.]"
)
#: Passage texts whose token counts a tool keeps (about 100 bytes each).
_PASSAGE_TOKEN_ENTRIES = 10_000
_CUT_NOTICE = (
    f"[knowledge_read: a passage longer than {MAX_MAX_TOKENS} tokens is cut, marked "
    f"{TRUNCATED_MARK}; the rest of it cannot be shown.]"
)


@dataclass(frozen=True)
class OpenDocument:
    """The document being read: its row, how the model refers to it (``D12``;
    its file name without a number), and its base."""

    row: KnowledgeDocument
    ref: str
    base: KnowledgeBase

    @property
    def spoken(self) -> str:
        """The name in a notice or error — never tenant text."""
        return self.ref if self.row.doc_seq is not None else "this document"


class PassageTokens:
    """Token counts for one tool's reads, never through the shared estimator's
    cache: that cache keeps every run's message prefixes warm, and a document's
    passages and labels written into it would evict them. :meth:`of` keeps a
    passage text's count here instead, keyed by a hash of the text (an edit or a
    re-ingest is a new key), so a document read page after page is counted once;
    :meth:`once` counts a short text (a label) without keeping it. Thread-safe:
    counting runs on worker threads."""

    def __init__(self, estimator: TokenEstimator, max_entries: int = _PASSAGE_TOKEN_ENTRIES):
        self._count: Callable[[str], int] = (
            estimator.count_uncached
            if isinstance(estimator, TiktokenEstimator)
            else estimator.count
        )
        self._max_entries = max_entries
        self._known: OrderedDict[bytes, int] = OrderedDict()
        self._lock = threading.Lock()

    def of(self, text: str) -> int:
        key = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
        with self._lock:
            known = self._known.get(key)
            if known is not None:
                self._known.move_to_end(key)
                return known
        tokens = self._count(text)
        with self._lock:
            self._known[key] = tokens
            while len(self._known) > self._max_entries:
                self._known.popitem(last=False)
        return tokens

    def once(self, text: str) -> int:
        return self._count(text)


@dataclass(frozen=True)
class _Shown:
    """One passage on the page: its position and view, role, and its text as
    shown (cut when over the ceiling)."""

    index: int
    passage: ReadPassage
    role: Role | None
    text: str
    tokens: int
    truncated: bool


@dataclass(frozen=True)
class DocumentReader:
    """One call's reads of one document's ``passages``."""

    tokens: PassageTokens
    doc: OpenDocument
    passages: list[ReadPassage]
    max_tokens: int
    several_bases: bool

    @property
    def budget(self) -> int:
        return int(self.max_tokens * PAGE_SHARE)

    async def page(self, page: int) -> ToolResult:
        shown = await self._show([(i, None) for i in range(len(self.passages))])
        pages = pack_pages([s.tokens for s in shown], self.budget)
        start, end = _pick(pages, page, f"{self.doc.spoken} has", self.max_tokens)
        position = f"page {page} of {len(pages)} at max_tokens {self.max_tokens}"
        nxt = _next_page(page, len(pages), "")
        notice = f"[knowledge_read: page {page} of {len(pages)} of {self.doc.spoken}{nxt}]"
        return self._result(
            "page", shown[start:end], position, notice, {"page": page}, pages=len(pages), page=page
        )

    async def around(self, chunk: int, context: int) -> ToolResult:
        target = next((i for i, p in enumerate(self.passages) if chunk in p.chunk_indexes), None)
        if target is None:
            numbers = [n for p in self.passages for n in p.chunk_indexes]
            span = f"its passages span #{min(numbers)}..#{max(numbers)}; " if numbers else ""
            msg = (
                f"{self.doc.spoken} has no readable passage containing #{chunk} ({span}numbers "
                "change when the document is re-ingested or a passage is switched off)"
            )
            raise GuidedToolError(msg, advice=_PASSAGE_ADVICE)
        lo, hi = max(0, target - context), min(len(self.passages), target + context + 1)
        window = [(i, _around_role(i, target)) for i in range(lo, hi)]
        shown = await self._show(window)
        before, after = around_window([s.tokens for s in shown], target - lo, context, self.budget)
        shown = shown[target - lo - before : target - lo + after + 1]
        reference = f"{self.doc.ref}#{chunk}"
        position = f"around {reference}: {_count(before, 'passage')} before, {after} after"
        notice = None
        if before < target - lo or after < hi - target - 1:
            notice = (
                f"[knowledge_read: {_count(before, 'passage')} before and {after} after fit "
                f"max_tokens {self.max_tokens} (context {context} was asked); a larger max_tokens, "
                f"up to {MAX_MAX_TOKENS}, fits more.]"
            )
        extra = {"context": context, "before": before, "after": after}
        return self._result(
            "around", shown, position, notice, {"context": context}, document=reference, **extra
        )

    async def section(self, section: str, page: int) -> ToolResult:
        wanted = wanted_path(section)
        if not wanted:
            msg = "knowledge_read section needs a heading"
            raise ValueError(msg)
        found = section_paths(self.passages, wanted)
        if len(found) != 1:
            return self._no_section(section, found)
        key, path = next(iter(found.items()))
        members = [(i, None) for i, p in enumerate(self.passages) if in_section(p, key)]
        shown = await self._show(members)
        pages = pack_pages([s.tokens for s in shown], self.budget)
        start, end = _pick(pages, page, "the section has", self.max_tokens)
        position = (
            f'section "{" > ".join(path)}": {_count(len(shown), "passage")} · '
            f"page {page} of {len(pages)}"
        )
        nxt = _next_page(page, len(pages), " with the same section")
        notice = f"[knowledge_read: page {page} of {len(pages)} of the section{nxt}]"
        return self._result(
            "section",
            shown[start:end],
            position,
            notice,
            {"section": section, "page": page},
            pages=len(pages),
            page=page,
        )

    async def find(self, find: str, page: int) -> ToolResult:
        words = list(
            dict.fromkeys(w for w in (squeeze(part) for part in normalize(find).split()) if w)
        )
        if not words or len(words) > MAX_FIND_WORDS or len(find) > MAX_ARGUMENT_CHARS:
            msg = (
                f"knowledge_read find takes 1 to {MAX_FIND_WORDS} words, at most "
                f"{MAX_ARGUMENT_CHARS} characters"
            )
            raise ValueError(msg)
        # Normalising every passage is the whole document's text: off the loop.
        matches, counts = await asyncio.to_thread(self._matching, words)
        if not matches:
            listed = "\n".join(f"{w}: {_count(n, 'passage')}" for w, n in counts)
            body = f"[no passage of {self.doc.ref} contains every word]\n{listed}"
            return self._listing(
                "find", "no_match", f'find "{find}": no passage', body, _NO_MATCH_NOTICE
            )
        # A page holds its hits whole (at most MAX_FIND_MATCHES); a neighbour
        # joins only when it fits what the page has left.
        hits = await self._show([(m, "match") for m in matches])
        pages = pack_pages([s.tokens for s in hits], self.budget, max_per_page=MAX_FIND_MATCHES)
        first, last = _pick(pages, page, "the find results have", self.max_tokens)
        on_page = hits[first:last]
        around = await self._show(neighbours(matches[first:last], len(self.passages), set(matches)))
        kept = fit_greedily(sum(s.tokens for s in on_page), [s.tokens for s in around], self.budget)
        shown = sorted([*on_page, *(around[i] for i in kept)], key=lambda s: s.index)
        verb = "contains" if len(matches) == 1 else "contain"
        position = (
            f'find "{find}": {_count(len(matches), "passage")} {verb} every word · '
            f"page {page} of {len(pages)}"
        )
        nxt = _next_page(page, len(pages), " with the same find")
        notice = (
            f"[knowledge_read: matches {first + 1}-{last} of the {len(matches)} passages that "
            f"contain every word (page {page} of {len(pages)}){nxt}]"
        )
        return self._result(
            "find",
            shown,
            position,
            notice,
            {"find": find, "page": page},
            pages=len(pages),
            page=page,
            matches=len(matches),
        )

    def _matching(self, words: Sequence[str]) -> tuple[list[int], list[tuple[str, int]]]:
        """The passages that contain every word, and — when none does — how many
        contain each word on its own."""
        haystacks = [haystack(p) for p in self.passages]
        matches = find_matches(haystacks, words)
        return matches, ([] if matches else word_counts(haystacks, words))

    def _no_section(
        self, section: str, found: Mapping[tuple[str, ...], tuple[str, ...]]
    ) -> ToolResult:
        if found:
            lines = [" > ".join(path) for path in found.values()][:MAX_LISTED_HEADINGS]
            body = (
                f'[several sections of {self.doc.ref} are headed "{section}"; pass the full '
                "heading path of one of them as section:]\n" + "\n".join(lines)
            )
            return self._listing(
                "section", "ambiguous", f'section "{section}"', body, _NO_SECTION_NOTICE
            )
        headings = outline(self.passages)
        if not headings:
            body = f"[{self.doc.ref} has no headings; read it by page or use find]"
        else:
            listed = [" > ".join(path) for path in headings[:MAX_LISTED_HEADINGS]]
            more = len(headings) - len(listed)
            body = f'[no section of {self.doc.ref} is headed "{section}"; its headings are:]\n'
            body += "\n".join(listed) + (f"\n… and {more} more" if more else "")
        return self._listing(
            "section", "no_section", f'section "{section}"', body, _NO_SECTION_NOTICE
        )

    async def _show(self, items: Sequence[tuple[int, Role | None]]) -> list[_Shown]:
        """``items`` (passage index, role) with their token sizes, counted off
        the event loop (a long document's first count is a lot of BPE work; the
        platform estimator is thread-safe: a lock guards its vocabulary load)."""
        return await asyncio.to_thread(self._measure, items)

    def _measure(self, items: Sequence[tuple[int, Role | None]]) -> list[_Shown]:
        separator = self.tokens.once(RESULT_SEPARATOR)
        shown: list[_Shown] = []
        for index, role in items:
            passage = self.passages[index]
            text, tokens, truncated = _fit(self.tokens, passage.text)
            head = self.tokens.once(_head(self.doc.ref, passage, role))
            shown.append(_Shown(index, passage, role, text, head + tokens + separator, truncated))
        return shown

    def _details(self) -> str:
        row = self.doc.row
        details: list[str] = []
        if row.title and row.title != _default_title(row.filename):
            details.append(f"title: {_one_line(row.title)}")
        if self.several_bases:
            details.append(f"base: {_one_line(self.doc.base.name)}")
        if row.created_at is not None:
            details.append(f"uploaded: {row.created_at.date().isoformat()}")
        for key, shown in _METADATA_DETAILS:
            value = row.metadata.get(key)
            if isinstance(value, str | int | float) and not isinstance(value, bool):
                details.append(f"{shown}: {_one_line(str(value))}")
        details.append(f"passages: {len(self.passages)}")
        return " · ".join(details)

    def _header(self, position: str) -> str:
        row = self.doc.row
        first = (
            f"[{self.doc.ref}] {row.filename}" if row.doc_seq is not None else f"[{row.filename}]"
        )
        return "\n".join([first, self._details(), position])

    def _notice(self, notice: str | None, shown: Sequence[_Shown]) -> str | None:
        lines = [notice] if notice else []
        if any(s.truncated for s in shown):
            lines.append(_CUT_NOTICE)
        if self.doc.row.status in (DocumentStatus.PENDING, DocumentStatus.PROCESSING):
            lines.append(_REINGEST_NOTICE)
        elif self.doc.row.has_pending:
            lines.append(_NOT_INGESTED_NOTICE)
        return "\n".join(lines) or None

    def _result(
        self,
        mode: str,
        shown: Sequence[_Shown],
        position: str,
        notice: str | None,
        args: Mapping[str, Any],
        *,
        document: str | None = None,
        **extra: Any,
    ) -> ToolResult:
        blocks = [f"{_head(self.doc.ref, s.passage, s.role)}{s.text}" for s in shown]
        content = f"{self._header(position)}\n\n{RESULT_SEPARATOR.join(blocks)}"
        meta: dict[str, Any] = {
            "status": "ok",
            "mode": mode,
            "document": self._meta_document(),
            "args": {"document": document or self.doc.ref, **args, "max_tokens": self.max_tokens},
            "max_tokens": self.max_tokens,
            "tokens": sum(s.tokens for s in shown),
            "passages": [_meta_passage(self.doc.ref, s) for s in shown],
            **extra,
        }
        return ToolResult(content=content, meta=meta, notice=self._notice(notice, shown))

    def _listing(self, mode: str, status: str, position: str, body: str, notice: str) -> ToolResult:
        """A read that found nothing to show: the details, why, and what to pass."""
        meta: dict[str, Any] = {
            "status": status,
            "mode": mode,
            "document": self._meta_document(),
            "max_tokens": self.max_tokens,
            "passages": [],
        }
        return ToolResult(
            content=f"{self._header(position)}\n\n{body}",
            meta=meta,
            notice=self._notice(notice, ()),
        )

    def _meta_document(self) -> dict[str, Any]:
        row = self.doc.row
        return {
            "id": str(row.id),
            "ref": self.doc.ref,
            "kb_id": str(row.kb_id),
            "status": row.status.value,
            "passages": len(self.passages),
        }


def _around_role(index: int, target: int) -> Role:
    if index == target:
        return "match"
    return "context_before" if index < target else "context_after"


def _head(ref: str, passage: ReadPassage, role: Role | None) -> str:
    mark = _MATCH_MARK if role == "match" else ""
    return f"[{label(ref, passage.chunk_indexes)}]{mark}\n"


def _fit(tokens: PassageTokens, text: str) -> tuple[str, int, bool]:
    """``text``, its tokens, and whether it was cut: a passage over
    :data:`MAX_MAX_TOKENS` is cut to it (by characters, shrunk until it fits)
    and marked."""
    full = tokens.of(text)
    if full <= MAX_MAX_TOKENS:
        return text, full, False
    keep = len(text) * MAX_MAX_TOKENS // full
    while keep > 0 and tokens.once(text[:keep]) > MAX_MAX_TOKENS:
        keep = keep * 9 // 10
    cut = f"{text[:keep]}\n{TRUNCATED_MARK}"
    return cut, tokens.once(cut), True


def _pick(
    pages: Sequence[tuple[int, int]], page: int, owner: str, max_tokens: int
) -> tuple[int, int]:
    if not 1 <= page <= len(pages):
        count = len(pages)
        msg = (
            f"page {page} is out of range: {owner} {_count(count, 'page')} at max_tokens "
            f"{max_tokens} (valid pages 1..{count})"
        )
        raise GuidedToolError(
            msg, advice=f"Pass a page from 1 to {count} with the same max_tokens."
        )
    return pages[page - 1]


def _next_page(page: int, pages: int, same: str) -> str:
    if page < pages:
        return f"; the next page is page={page + 1}{same}."
    return "; this is the last page."


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _meta_passage(ref: str, shown: _Shown) -> dict[str, Any]:
    return {
        "id": str(shown.passage.id),
        "ref": label(ref, shown.passage.chunk_indexes),
        "chunk_indexes": list(shown.passage.chunk_indexes),
        "role": shown.role,
        "tokens": shown.tokens,
        "truncated": shown.truncated,
    }


def _default_title(filename: str) -> str:
    """The title a store gives a file by default: its name without the last
    extension (``DocumentFileFacts``)."""
    suffix = PurePosixPath(filename).suffix
    return filename[: -len(suffix)] if suffix and len(suffix) < len(filename) else filename


def _one_line(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _DETAIL_MAX_CHARS else flat[: _DETAIL_MAX_CHARS - 1] + "…"
