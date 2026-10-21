"""What one ``knowledge_search`` result shows the model (B-206 K-4).

The tool hands this module the retriever's ranked passages, best first. It
picks the ones to show — at most :data:`MAX_PER_DOCUMENT` per document, so one
document whose title matches the query cannot crowd out the rest (Onyx
``MAX_CHUNKS_FOR_RELEVANCE`` = 3), the requested count, and at most
:data:`RESULT_TOKEN_BUDGET` tokens by the platform estimator, dropping the
lowest-ranked first and always keeping the first — renders them grouped by
document under a short ``[doc N]`` id, and describes every ranked passage for
the trajectory panel (``ToolResult.meta``, never the model's input). No ids or
scores reach the text. A passage is a retrieved chunk today; with parent
chunks (K-1) it becomes the parent and nothing here changes shape. The budget
is a platform default: no existing knowledge-base field can hold it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from expert_work.protocol import DocumentSourceType
from expert_work.runtime.tokens import TokenEstimator

if TYPE_CHECKING:
    # Typing only: ``orchestrator.tools.knowledge`` imports this module.
    from orchestrator.tools.knowledge import RetrievedChunk

MAX_PER_DOCUMENT = 3
RESULT_TOKEN_BUDGET = 8_000
#: How the text is joined: passages under their document's header, one
#: section per document. Counted into the budget like the blocks they join.
_PASSAGE_SEPARATOR = "\n\n"
_DOCUMENT_SEPARATOR = "\n\n---\n\n"
#: ``metadata`` keys a document header shows when present, with their labels —
#: the platform's convention for these three (K-15 fills them automatically).
_HEADER_METADATA = (("version", "version"), ("effective_date", "effective"), ("source", "source"))
_METADATA_VALUE_MAX_CHARS = 80
#: Marks the end of a first passage cut to fit the budget (a base chunked far
#: larger than the budget, or an oversized legacy chunk).
_PASSAGE_CUT_NOTE = "\n[... passage cut here to fit the result size]"

#: Why a ranked passage is not shown.
DropReason = Literal["per_document", "limit", "budget"]


@dataclass(frozen=True)
class PassageSelection:
    """Which ranked passages one result shows.

    ``shown`` best first, the first one possibly cut to fit the budget
    (``first_cut``); ``dropped`` why each input passage is not shown, aligned
    with the input (``None`` = shown); ``tokens`` the shown text's size by the
    estimator, separators included."""

    shown: tuple[RetrievedChunk, ...]
    dropped: tuple[DropReason | None, ...]
    tokens: int
    first_cut: bool = False

    def left_out(self, reason: DropReason) -> int:
        return sum(1 for dropped in self.dropped if dropped == reason)


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
    return PassageSelection(
        shown=tuple(shown), dropped=tuple(dropped), tokens=tokens, first_cut=first_cut
    )


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
    results: Sequence[RetrievedChunk], selection: PassageSelection, *, limit: int
) -> dict[str, Any]:
    """Hit details for the trajectory panel: every ranked passage with its ids,
    four scores and whether / why it was not shown. JSON-safe — it rides the
    ToolMessage artifact into the checkpoint, audit and trace."""
    return {
        "hits": len(selection.shown),
        "relevant": len(results),
        "limit": limit,
        "truncated": len(selection.shown) < len(results),
        "result_tokens": selection.tokens,
        "token_budget": RESULT_TOKEN_BUDGET,
        "first_passage_cut": selection.first_cut,
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
    block fits ``budget`` tokens."""
    overhead = _passage_cost(replace(result, content=""), estimator) + estimator.count(
        _PASSAGE_CUT_NOTE
    )
    room = max(0, budget - overhead)
    keep = len(result.content) * room // estimator.count(result.content)
    return replace(result, content=result.content[:keep] + _PASSAGE_CUT_NOTE)


def _document_key(result: RetrievedChunk) -> object:
    return result.document.id if result.document is not None else result.filename


def _document_label(result: RetrievedChunk) -> str:
    """What the model calls a document: ``doc N`` by its tenant-wide short
    number, shared by all its passages (the file name for a row without one)."""
    document = result.document
    if document is not None and document.doc_seq is not None:
        return f"doc {document.doc_seq}"
    return _one_line(result.filename)


def _document_header(result: RetrievedChunk) -> str:
    """``[doc N] title (file) · uploaded date · version / effective / source ·
    source type`` — each part only when recorded; the title only when it is
    more than the file name without its extension."""
    document = result.document
    if document is None:
        return f"[{_document_label(result)}]"
    filename = _one_line(document.filename)
    title = _one_line(document.title or "")
    name = filename if not title or filename.startswith(title) else f"{title} ({filename})"
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


def _passage_block(result: RetrievedChunk) -> str:
    """``[doc N · passage n/N]`` then the passage's section and body. ``N`` is
    left out when the document's chunk count does not cover the passage."""
    position = result.chunk_index + 1
    total = result.document.chunk_count if result.document is not None else 0
    where = f"passage {position}/{total}" if position <= total else f"passage {position}"
    return f"[{_document_label(result)} · {where}]\n{result.retrieval_text}"


def _metadata_text(value: object) -> str | None:
    """A ``metadata`` value as one bounded line; ``None`` for anything but a
    non-empty string or number."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text = _one_line(str(value))
    if len(text) > _METADATA_VALUE_MAX_CHARS:
        text = text[: _METADATA_VALUE_MAX_CHARS - 1] + "…"
    return text or None


def _one_line(text: str) -> str:
    return " ".join(text.split())


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
        "vector_score": result.vector_score,
        "keyword_score": result.keyword_score,
        "fused_score": result.fused_score,
        "rerank_score": result.rerank_score,
    }
