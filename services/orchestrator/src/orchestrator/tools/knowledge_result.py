"""What one ``knowledge_search`` result shows the model (B-206 K-4).

The tool hands this module the retriever's ranked passages, best first. It
picks the ones to show — at most :data:`MAX_PER_DOCUMENT` per document, so one
document whose title matches the query cannot crowd out the rest (Onyx
``MAX_CHUNKS_FOR_RELEVANCE`` = 3), the requested count, and at most
:data:`RESULT_TOKEN_BUDGET` tokens by the platform estimator, dropping the
lowest-ranked first and always keeping the first — renders them grouped by
document under a short ``[doc N]`` id, and describes every ranked passage for
the trajectory panel (``ToolResult.meta``, never the model's input). The
``[doc N]`` short id (``doc_seq``) is meant for the model; no UUIDs or scores
reach the text. The budget is a hard bound on the rendered text: this tool's
result skips the tools node's generic preview, so nothing downstream cuts it.
A passage is a retrieved chunk today; with parent chunks (K-1) it becomes the
parent and nothing here changes shape. The budget is a platform default: no
existing knowledge-base field can hold it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Literal

from expert_work.protocol import DocumentSourceType, chunk_retrieval_text
from expert_work.runtime.tokens import TokenEstimator
from orchestrator.tools.knowledge_types import RetrievedChunk

MAX_PER_DOCUMENT = 3
RESULT_TOKEN_BUDGET = 8_000
#: How the text is joined: passages under their document's header, one
#: section per document. Counted into the budget like the blocks they join.
_PASSAGE_SEPARATOR = "\n\n"
_DOCUMENT_SEPARATOR = "\n\n---\n\n"
#: ``metadata`` keys a document header shows when present, with their labels —
#: the platform's convention for these three (K-15 fills them automatically).
_HEADER_METADATA = (("version", "version"), ("effective_date", "effective"), ("source", "source"))
#: Longest file name / title / metadata value / section path shown: they are
#: tenant-supplied and repeat on every passage, so a long one must not eat the
#: budget the passages are for.
_NAME_MAX_CHARS = 120
_METADATA_VALUE_MAX_CHARS = 80
_SECTION_MAX_CHARS = 200
#: Marks the end of a first passage cut to fit the budget (a base chunked far
#: larger than the budget, or an oversized legacy chunk).
_PASSAGE_CUT_NOTE = "\n[... passage cut here to fit the result size]"
#: Marks a rendered result trimmed to the budget as a last resort.
_RESULT_CUT_NOTE = "\n[... result cut here to fit the result size]"

#: Why a ranked passage is not shown.
DropReason = Literal["per_document", "limit", "budget"]


@dataclass(frozen=True)
class PassageSelection:
    """Which ranked passages one result shows.

    ``shown`` best first, the first one possibly cut to fit the budget
    (``first_cut``); ``dropped`` why each input passage is not shown, aligned
    with the input (``None`` = shown)."""

    shown: tuple[RetrievedChunk, ...]
    dropped: tuple[DropReason | None, ...]
    first_cut: bool = False

    def left_out(self, reason: DropReason) -> int:
        return sum(1 for dropped in self.dropped if dropped == reason)


@dataclass(frozen=True)
class RenderedResult:
    """The text the model reads, its size by the estimator, and whether it had
    to be trimmed to the budget after rendering (``cut``)."""

    text: str
    tokens: int
    cut: bool = False


def select_passages(
    results: Sequence[RetrievedChunk], *, limit: int, estimator: TokenEstimator
) -> PassageSelection:
    """Walk ``results`` best first: skip a passage whose document already shows
    :data:`MAX_PER_DOCUMENT`, stop adding at ``limit``, and at the first passage
    that would take the text over :data:`RESULT_TOKEN_BUDGET` drop it and every
    lower-ranked one — dropping from the bottom until the rest fits. The first
    passage is always shown, cut to the budget when it alone is over it."""
    budget = RESULT_TOKEN_BUDGET
    shown: list[RetrievedChunk] = []
    dropped: list[DropReason | None] = []
    per_document: dict[object, int] = {}
    tokens = 0
    first_cut = False
    budget_reached = False
    for result in results:
        key = _document_key(result)
        if per_document.get(key, 0) >= MAX_PER_DOCUMENT:
            dropped.append("per_document")
            continue
        if len(shown) >= limit:
            dropped.append("limit")
            continue
        if budget_reached:
            dropped.append("budget")
            continue
        header = 0 if key in per_document else _header_cost(result, estimator)
        cost = header + _passage_cost(result, estimator)
        if shown and tokens + cost > budget:
            budget_reached = True
            dropped.append("budget")
            continue
        if cost > budget:
            result = _cut_passage(result, budget - header, estimator)
            cost = header + _passage_cost(result, estimator)
            first_cut = True
        shown.append(result)
        dropped.append(None)
        per_document[key] = per_document.get(key, 0) + 1
        tokens += cost
    return PassageSelection(shown=tuple(shown), dropped=tuple(dropped), first_cut=first_cut)


def render(selection: PassageSelection, *, estimator: TokenEstimator) -> RenderedResult:
    """:func:`format_results` of the shown passages, held to
    :data:`RESULT_TOKEN_BUDGET` by the estimator. Selection sums block by block,
    which a fixed overhead (a header, the cut note) or a tokenizer merging across
    blocks can still overrun, so the rendered text is measured once more and, if
    over, trimmed — the last cut this result gets."""
    budget = RESULT_TOKEN_BUDGET
    text = format_results(selection.shown)
    tokens = estimator.count(text)
    if tokens <= budget:
        return RenderedResult(text=text, tokens=tokens)
    keep = len(text) * budget // max(1, tokens)
    while True:
        trimmed = text[:keep] + _RESULT_CUT_NOTE
        tokens = estimator.count(trimmed)
        if tokens <= budget or keep == 0:
            return RenderedResult(text=trimmed, tokens=tokens, cut=True)
        keep = keep * 9 // 10


def format_results(results: Sequence[RetrievedChunk]) -> str:
    """The text the model reads: ``results`` (best first) grouped by document —
    documents in the order of their best passage, each one's header once, then
    its passages in reading order."""
    groups: dict[object, list[RetrievedChunk]] = {}
    for result in results:
        groups.setdefault(_document_key(result), []).append(result)
    sections = [
        _PASSAGE_SEPARATOR.join(
            [
                _document_header(group[0]),
                *(_passage_block(r) for r in sorted(group, key=lambda r: r.chunk_index)),
            ]
        )
        for group in groups.values()
    ]
    return _DOCUMENT_SEPARATOR.join(sections)


def selection_meta(
    results: Sequence[RetrievedChunk],
    selection: PassageSelection,
    rendered: RenderedResult,
    *,
    limit: int,
) -> dict[str, Any]:
    """Hit details for the trajectory panel: every ranked passage with its ids,
    four scores and whether / why it was not shown. JSON-safe — it rides the
    ToolMessage artifact into the checkpoint, audit and trace."""
    return {
        "hits": len(selection.shown),
        "relevant": len(results),
        "limit": limit,
        "truncated": len(selection.shown) < len(results),
        "result_tokens": rendered.tokens,
        "token_budget": RESULT_TOKEN_BUDGET,
        "first_passage_cut": selection.first_cut,
        "result_cut": rendered.cut,
        "results": [
            _hit_meta(rank, result, dropped)
            for rank, (result, dropped) in enumerate(
                zip(results, selection.dropped, strict=True), start=1
            )
        ],
    }


def truncation_notice(selection: PassageSelection, *, limit: int, max_limit: int) -> str | None:
    """The trailer — only when ranked passages were left out (WeKnora
    ``RetrievalTruncation``): how many, why, and what can show them. Facts,
    not commands (B-163); counts only, no document text."""
    found = len(selection.dropped)
    shown = len(selection.shown)
    if shown == found:
        return None
    reasons: list[str] = []
    ways: list[str] = []
    if count := selection.left_out("per_document"):
        reasons.append(
            f"{count} beyond the {MAX_PER_DOCUMENT} passages one search shows per document"
        )
    if count := selection.left_out("limit"):
        reasons.append(f"{count} beyond the {limit} requested")
        ways.append(f"a larger limit (up to {max_limit})")
    if count := selection.left_out("budget"):
        reasons.append(f"{count} beyond the result size")
    if selection.left_out("per_document") or selection.left_out("budget"):
        ways.append("a narrower search (for example in one document's own terms)")
    way = " or ".join(ways)
    return (
        f"[knowledge_search: showing {shown} of the {found} top passages this search found; "
        f"left out: {', '.join(reasons)}. {way[0].upper()}{way[1:]} can show the ones left "
        "out. These counts cover this search's top passages, not every match in the "
        "knowledge base.]"
    )


def _header_cost(result: RetrievedChunk, estimator: TokenEstimator) -> int:
    return estimator.count(_DOCUMENT_SEPARATOR) + estimator.count(_document_header(result))


def _passage_cost(result: RetrievedChunk, estimator: TokenEstimator) -> int:
    return estimator.count(_PASSAGE_SEPARATOR) + estimator.count(_passage_block(result))


def _cut_passage(result: RetrievedChunk, budget: int, estimator: TokenEstimator) -> RetrievedChunk:
    """``result`` with its body cut, in proportion to its token count, so its
    block fits ``budget`` tokens (:func:`render` makes the bound exact)."""
    overhead = _passage_cost(replace(result, content=""), estimator) + estimator.count(
        _PASSAGE_CUT_NOTE
    )
    room = max(0, budget - overhead)
    keep = len(result.content) * room // max(1, estimator.count(result.content))
    return replace(result, content=result.content[:keep] + _PASSAGE_CUT_NOTE)


def _document_key(result: RetrievedChunk) -> object:
    return result.document.id if result.document is not None else result.filename


def _document_label(result: RetrievedChunk) -> str:
    """What the model calls a document: ``doc N`` by its tenant-wide short
    number, shared by all its passages (the file name for a row without one)."""
    document = result.document
    if document is not None and document.doc_seq is not None:
        return f"doc {document.doc_seq}"
    return _clip(result.filename, _NAME_MAX_CHARS)


def _document_header(result: RetrievedChunk) -> str:
    """``[doc N] title (file) · uploaded date · version / effective / source ·
    source type`` — each part only when recorded; the title only when it says
    more than the file name (see :func:`_title_adds`)."""
    document = result.document
    if document is None:
        return f"[{_document_label(result)}]"
    filename = _clip(document.filename, _NAME_MAX_CHARS)
    title = _one_line(document.title or "")
    name = (
        f"{_clip(title, _NAME_MAX_CHARS)} ({filename})"
        if _title_adds(title, _one_line(document.filename))
        else filename
    )
    head = f"[doc {document.doc_seq}] {name}" if document.doc_seq is not None else f"[{name}]"
    fields = [head]
    if document.created_at is not None:
        fields.append(f"uploaded {document.created_at.date().isoformat()}")
    for key, label in _HEADER_METADATA:
        value = _metadata_text(document.metadata.get(key))
        if value is not None:
            fields.append(f"{label} {value}")
    if document.source_type is not DocumentSourceType.FILE:
        fields.append(f"source type {document.source_type.value}")
    return " · ".join(fields)


def _title_adds(title: str, filename: str) -> bool:
    """Whether ``title`` says more than ``filename``: not when it is the file
    name, the file name without its last extension (the title a store derives
    on upload, ``DocumentFileFacts``), or that name's bare stem."""
    path = PurePosixPath(filename)
    derived = filename[: -len(path.suffix)] if path.suffix else filename
    return bool(title) and title not in {filename, derived, path.stem}


def _passage_block(result: RetrievedChunk) -> str:
    """``[doc N · passage n/N]`` then the passage's section and body. ``N`` is
    left out when the document's chunk count does not cover the passage."""
    position = result.chunk_index + 1
    total = result.document.chunk_count if result.document is not None else 0
    where = f"passage {position}/{total}" if position <= total else f"passage {position}"
    section = (
        (_clip(" > ".join(result.heading_path), _SECTION_MAX_CHARS),) if result.heading_path else ()
    )
    return f"[{_document_label(result)} · {where}]\n{chunk_retrieval_text(section, result.content)}"


def _metadata_text(value: object) -> str | None:
    """A ``metadata`` value as one bounded line; ``None`` for anything but a
    non-empty string or number."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    return _clip(str(value), _METADATA_VALUE_MAX_CHARS) or None


def _clip(text: str, max_chars: int) -> str:
    """``text`` on one line, cut to ``max_chars`` with a trailing ``…``."""
    line = _one_line(text)
    return line if len(line) <= max_chars else line[: max_chars - 1] + "…"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _finite(score: float | None) -> float | None:
    """``score``, or ``None`` when it is NaN / infinite: the meta is stored as
    JSON (Postgres JSONB rejects both)."""
    return score if score is not None and math.isfinite(score) else None


def _hit_meta(rank: int, result: RetrievedChunk, dropped: DropReason | None) -> dict[str, Any]:
    document = result.document
    return {
        "rank": rank,
        "shown": dropped is None,
        "dropped": dropped,
        "chunk_id": None if result.chunk_id is None else str(result.chunk_id),
        "chunk_index": result.chunk_index,
        "document_id": None if document is None else str(document.id),
        "doc_seq": None if document is None else document.doc_seq,
        "filename": result.filename,
        "recall_source": result.recall_source,
        "vector_score": _finite(result.vector_score),
        "keyword_score": _finite(result.keyword_score),
        "fused_score": _finite(result.fused_score),
        "rerank_score": _finite(result.rerank_score),
    }
