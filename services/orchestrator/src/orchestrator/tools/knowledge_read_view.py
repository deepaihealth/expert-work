"""What ``knowledge_read`` shows of a document — pure functions (B-206 K-2).

A document is read as its passages (:class:`ReadPassage`: a parent row, or a
chunk standing for itself), in reading order. Each mode picks some of them —
a page (:func:`pack_pages`), the window around one (:func:`around_window`),
those under a heading (:func:`section_paths` / :func:`in_section`), those that
contain given words (:func:`find_units`) — and the tool lays them out. Sizes are
token counts the tool measured; nothing here reads a store or counts tokens.

A passage is labelled by the chunk numbers under it (``D12#7-9``): a
``knowledge_search`` result names its best chunk (``D12#8``), and any number in
the label points at the same passage.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

#: How a passage relates to what was asked: the passage asked for (around) or
#: one that contains every word (find), or a neighbour shown with it. Page and
#: section reads mark none.
Role = Literal["match", "context_before", "context_after"]

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ReadPassage:
    """One readable passage: its id, the numbers of the chunks under it (sorted;
    empty for a parent row with no chunk left), its text as the model reads it
    (breadcrumb + body), and the breadcrumbs of its chunks (its own when it has
    none) — the headings a section read and find look at."""

    id: UUID
    chunk_indexes: tuple[int, ...]
    text: str
    heading_paths: tuple[tuple[str, ...], ...]


def label(doc_ref: str, indexes: Sequence[int]) -> str:
    """``doc_ref`` and the passage's chunk numbers as runs: ``D12#7-9,41``."""
    if not indexes:
        return doc_ref
    runs: list[str] = []
    start = prev = indexes[0]
    for index in [*indexes[1:], None]:
        if index is not None and index == prev + 1:
            prev = index
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        if index is not None:
            start = prev = index
    return f"{doc_ref}#{','.join(runs)}"


def normalize(text: str) -> str:
    """``text`` compared without case or width: NFKC, case-folded, whitespace
    runs as one space, trimmed."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def squeeze(text: str) -> str:
    """:func:`normalize` without any whitespace — so ``17个月`` finds ``17 个月``."""
    return _WHITESPACE.sub("", normalize(text))


def pack_pages(
    sizes: Sequence[int], budget: int, *, max_per_page: int | None = None
) -> list[tuple[int, int]]:
    """``sizes`` (units in reading order) cut into pages ``[start, end)``: each
    page takes units while their total stays within ``budget``, and at most
    ``max_per_page`` of them. A page always takes at least one unit, however
    large, so every unit is on some page."""
    pages: list[tuple[int, int]] = []
    start, used = 0, 0
    for i, size in enumerate(sizes):
        full = max_per_page is not None and i - start >= max_per_page
        if i > start and (used + size > budget or full):
            pages.append((start, i))
            start, used = i, 0
        used += size
    if start < len(sizes):
        pages.append((start, len(sizes)))
    return pages


def around_window(sizes: Sequence[int], target: int, context: int, budget: int) -> tuple[int, int]:
    """How many passages before and after ``target`` to show: up to ``context``
    on each side, nearest first, before then after, while the total stays
    within ``budget``. A side stops at its first passage that does not fit, so
    what is shown stays contiguous; ``target`` is always shown."""
    used = sizes[target]
    before = after = 0
    open_before = open_after = True
    for distance in range(1, context + 1):
        if open_before:
            j = target - distance
            if j >= 0 and used + sizes[j] <= budget:
                used += sizes[j]
                before = distance
            else:
                open_before = False
        if open_after:
            j = target + distance
            if j < len(sizes) and used + sizes[j] <= budget:
                used += sizes[j]
                after = distance
            else:
                open_after = False
    return before, after


def wanted_path(section: str) -> tuple[str, ...]:
    """A ``section`` argument as normalized headings: ``A > B`` is a path."""
    return tuple(part for part in (normalize(p) for p in section.split(">")) if part)


def section_paths(
    passages: Sequence[ReadPassage], wanted: tuple[str, ...]
) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Every distinct breadcrumb that ends in ``wanted`` (its headings in a row,
    at any depth), normalized → as written: the sections the argument names."""
    found: dict[tuple[str, ...], tuple[str, ...]] = {}
    size = len(wanted)
    for passage in passages:
        for path in passage.heading_paths:
            normal = tuple(normalize(h) for h in path)
            for end in range(size, len(normal) + 1):
                if normal[end - size : end] == wanted:
                    found.setdefault(normal[:end], path[:end])
    return found


def in_section(passage: ReadPassage, key: tuple[str, ...]) -> bool:
    """Whether a chunk of ``passage`` sits under the section ``key``."""
    return any(
        tuple(normalize(h) for h in path[: len(key)]) == key for path in passage.heading_paths
    )


def outline(passages: Sequence[ReadPassage], depth: int = 2) -> list[tuple[str, ...]]:
    """The document's distinct breadcrumbs cut to ``depth``, in reading order."""
    seen: dict[tuple[str, ...], None] = {}
    for passage in passages:
        for path in passage.heading_paths:
            if path:
                seen.setdefault(path[:depth], None)
    return list(seen)


def haystack(passage: ReadPassage) -> str:
    """What find looks in: the passage's text and every chunk's breadcrumb
    (a heading can live only in a breadcrumb), squeezed."""
    headings = " ".join(" ".join(path) for path in passage.heading_paths)
    return squeeze(f"{headings}\n{passage.text}")


def find_units(haystacks: Sequence[str], words: Sequence[str]) -> list[list[tuple[int, Role]]]:
    """The passages that contain every one of ``words`` (squeezed), each as a
    unit with the passage before and after it — a neighbour already shown, or
    that is itself a match, is not repeated. In reading order."""
    matches = [i for i, text in enumerate(haystacks) if all(w in text for w in words)]
    matched = set(matches)
    shown: set[int] = set()
    units: list[list[tuple[int, Role]]] = []
    for m in matches:
        unit: list[tuple[int, Role]] = []
        if m - 1 >= 0 and m - 1 not in matched and m - 1 not in shown:
            unit.append((m - 1, "context_before"))
        unit.append((m, "match"))
        if m + 1 < len(haystacks) and m + 1 not in matched:
            unit.append((m + 1, "context_after"))
        shown.update(i for i, _role in unit)
        units.append(unit)
    return units


def word_counts(haystacks: Sequence[str], words: Sequence[str]) -> list[tuple[str, int]]:
    """How many passages contain each word on its own."""
    return [(w, sum(w in text for text in haystacks)) for w in words]
