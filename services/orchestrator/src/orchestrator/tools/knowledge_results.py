"""How a knowledge search's ranked chunks become what the model reads (B-206 K-1).

Recall, fusion, rerank and the rerank-score floor all run on child chunks, the
small units that are embedded and keyword-indexed. A result is a child's parent,
the larger passage the child was cut from: :func:`collapse_to_parents` walks the
ranked children best first and keeps each parent once, at its best child's place,
with the index of every child of it that ranked; ``limit`` counts parents.
:func:`lay_out` then lays the results out within one call's text budget.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from expert_work.protocol import KnowledgeChunk

#: The most text one ``knowledge_search`` result hands the model, in characters,
#: headers, separators and marks included (so the bound holds whatever the file
#: names). It was set under ``EXTERNALIZE_MIN_CHARS`` (12,000, ``tools/overflow.py``)
#: while a longer result would have been swapped for a 3,000 character preview.
#: Since B-206 track D ``knowledge_search`` is exempt from externalization
#: (``EXEMPT_TOOLS``): nothing is written to the workspace and an old result
#: collapses to a reference line instead, so this budget alone bounds what one
#: search puts in context. Characters, as the tool output is measured.
RESULTS_TEXT_BUDGET = 10_000
#: Ends a result that shows its hit child in place of a parent too long for what
#: is left of the budget.
TRUNCATED_MARK = "[已截断]"
RESULT_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class ParentHit:
    """One result before its parent row is read: ``best`` the best-ranked child
    of the parent, whose place and scores the result takes, and the index of
    every ranked child of that parent, best first."""

    best: KnowledgeChunk
    hit_chunk_indexes: tuple[int, ...]


def collapse_to_parents(
    children: Sequence[KnowledgeChunk], limit: int | None = None
) -> list[ParentHit]:
    """The best ``limit`` parents of ``children`` (ranked best first; every one
    when ``limit`` is ``None``). A child's parent is its ``parent_chunk_id``, or
    the child itself when it has none (no parent row was stored for it). A later
    child of a parent already seen only adds its index to that parent's hits."""
    best: dict[UUID, KnowledgeChunk] = {}
    hits: dict[UUID, list[int]] = {}
    for child in children:
        key = child.parent_chunk_id or child.id
        best.setdefault(key, child)
        hits.setdefault(key, []).append(child.chunk_index)
    return [ParentHit(best[key], tuple(hits[key])) for key in list(best)[:limit]]


@dataclass(frozen=True)
class ResultText:
    """One result as the budget sees it: its ``[D12#7] <file>`` header, its full
    text (the parent's), and its hit child's text, the shorter stand-in when the
    full text does not fit (``None`` when the full text is the hit child's own)."""

    header: str
    text: str
    hit_text: str | None = None


@dataclass(frozen=True)
class LaidOut:
    """:func:`lay_out`'s text, whether any result in it shows less than its
    full text (B-206 K-2: the tool then points at ``knowledge_read``), and how
    many results it shows (the first ``shown``; lower ones were dropped)."""

    text: str
    truncated: bool
    shown: int


def lay_out(results: Sequence[ResultText], budget: int = RESULTS_TEXT_BUDGET) -> LaidOut:
    """``results`` in order, within ``budget`` characters. A result whose full text
    does not fit what is left shows its hit child instead, marked
    :data:`TRUNCATED_MARK`; when that does not fit either, the listing stops there:
    lower-ranked results are dropped, never skipped over. The first result always
    appears, its hit child cut to the budget when even that is longer."""
    blocks: list[str] = []
    truncated = False
    room = budget
    for result in results:
        if blocks:
            room -= len(RESULT_SEPARATOR)
        block = _fitting_block(result, room)
        if block is None:
            if blocks:
                break
            block = _cut_block(result, room)
        truncated = truncated or block != _full_block(result)
        blocks.append(block)
        room -= len(block)
    return LaidOut(RESULT_SEPARATOR.join(blocks), truncated, len(blocks))


def _full_block(result: ResultText) -> str:
    return f"{result.header}\n{result.text}"


def _fitting_block(result: ResultText, room: int) -> str | None:
    """``result`` in full if that fits ``room``, else its hit child marked
    truncated if that fits, else ``None``."""
    full = _full_block(result)
    if len(full) <= room:
        return full
    if result.hit_text is None:
        return None
    truncated = _truncated(result.header, result.hit_text)
    return truncated if len(truncated) <= room else None


def _cut_block(result: ResultText, room: int) -> str:
    """The first result when not even its hit child fits: the hit child (the
    result's own text when it has no parent) cut to ``room``, marked truncated."""
    text = result.hit_text if result.hit_text is not None else result.text
    overhead = len(_truncated(result.header, ""))
    return _truncated(result.header, text[: max(0, room - overhead)])


def _truncated(header: str, text: str) -> str:
    return f"{header}\n{text}\n{TRUNCATED_MARK}"
