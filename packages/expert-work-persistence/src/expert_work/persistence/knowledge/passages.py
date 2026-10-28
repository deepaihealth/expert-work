"""Passages — the types and rules both knowledge stores share (B-175 P3-PR2).

A passage is what the console shows, edits and switches off as one unit: a
parent row (B-206 K-1) with the chunks searched under it, or a chunk with no
parent row that stands for itself. The pure rules here (how a passage's text
is laid out as rows, when it counts as edited, which histories are unapplied,
which passages a re-ingest must carry over) are used by both
:class:`~expert_work.persistence.knowledge.sql.SqlKnowledgeStore` and
:class:`~expert_work.persistence.knowledge.memory.InMemoryKnowledgeStore`, so
the two cannot drift. Re-exported from :mod:`expert_work.persistence.knowledge.base`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from expert_work.protocol import ChunkType, KnowledgeChunk

#: Why a disabled row is off (``knowledge_chunk.disabled_reason``).
DisabledReason = Literal["manual", "inherited"]


@dataclass(frozen=True)
class Passage:
    """One passage of a document: a parent row with the chunks searched
    under it, or a chunk with no parent row that stands for itself
    (``children == (row,)``). Vectors are not loaded."""

    row: KnowledgeChunk
    children: tuple[KnowledgeChunk, ...]

    @property
    def id(self) -> UUID:
        return self.row.id


@dataclass(frozen=True)
class PassageChild:
    """One chunk of a passage's new text, as an edit writes it: the store
    numbers it (and gives it an id); the caller cut, hashed and embedded it
    (``doc_title`` is the title its retrieval text was built with)."""

    content: str
    heading_path: tuple[str, ...]
    content_hash: str | None
    embedding: Sequence[float]
    embedding_model: str | None
    doc_title: str | None = None


@dataclass(frozen=True)
class ChunkVector:
    """A re-index's new vector for one chunk, with the ``content_hash`` the
    chunk had when it was read: the write-back only lands while the chunk
    still has that id and text."""

    chunk_id: UUID
    content_hash: str | None
    embedding: Sequence[float]


@dataclass(frozen=True)
class PassageRevision:
    """One ``knowledge_chunk_revision`` row: a passage's text at one revision.
    The first row of a passage ingested from the file is its machine text and
    breadcrumb (``edited_by`` ``None``); one added by hand starts with its
    author's. ``orphaned_at``: when a re-ingest left the passage out.

    ``switch_off_lost``: not a text revision but a **switch-off record**
    (revision :data:`SWITCH_OFF_REVISION`): a person switched this passage
    off, and a re-ingest could not keep the switch-off on it (the passage is
    gone). ``content`` / ``heading_path`` are the machine text it hides —
    every ingest switches off (``inherited``) the passages holding that text,
    derived afresh from these records alone. ``orphaned_at`` is then the time
    since which no passage holds any of it (``None`` while one does); such a
    record is listed for review with the unapplied edits."""

    passage_id: UUID
    revision: int
    content: str
    heading_path: tuple[str, ...]
    edited_by: str | None
    created_at: datetime | None = None
    orphaned_at: datetime | None = None
    switch_off_lost: bool = False


#: The revision number of a switch-off record (text revisions start at 1).
SWITCH_OFF_REVISION = 0


@dataclass(frozen=True)
class SwitchOffRecord:
    """A switch-off record as an ingest writes it (``replace_chunks``):
    whose switch-off (the gone passage's id), the text it hides, and whether
    a passage of the new chunks holds any of it (``placed``)."""

    passage_id: UUID
    content: str
    heading_path: tuple[str, ...]
    placed: bool


@dataclass(frozen=True)
class ManualPassage:
    """A passage carrying a manual change a re-ingest must carry over: edited,
    switched off, added by hand, or with a revision history. ``original`` is
    its first revision (``None`` when it has none: switched off, never
    edited). ``occurrence``: how many passages of the document before it
    have the same machine text and breadcrumb (counted over every passage,
    manual or not) — which of identical copies it is. ``unique_text``: no
    other passage of the document has its machine text, under any
    breadcrumb. ``user_heading``: the breadcrumb of its latest revision by a
    person (``None``: nobody wrote it) — unlike the row's, a re-ingest never
    moves it."""

    row: KnowledgeChunk
    original: PassageRevision | None
    occurrence: int = 0
    unique_text: bool = True
    user_heading: tuple[str, ...] | None = None

    @property
    def added(self) -> bool:
        """Added by hand: its first revision has an author."""
        return self.original is not None and self.original.edited_by is not None

    @property
    def anchor(self) -> str | None:
        """The machine text the passage was cut as — what a re-ingest finds it
        by; ``None`` for a passage added by hand (it has none)."""
        if self.original is None:
            return self.row.content
        return None if self.added else self.original.content

    @property
    def anchor_heading(self) -> tuple[str, ...]:
        """The breadcrumb the passage was cut under (with :attr:`anchor`)."""
        if self.original is None or self.added:
            return self.row.heading_path
        return self.original.heading_path


class PassageConflictError(Exception):
    """A passage write refused (the API answers 409): ``revision_conflict``
    (the passage changed since the caller read it; ``current`` is how it
    stands now) or ``document_busy`` (the document is being ingested or a new
    version of it waits — its passages are about to be replaced)."""

    def __init__(
        self,
        reason: Literal["revision_conflict", "document_busy"],
        current: Passage | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current = current


@dataclass(frozen=True)
class PassageLayout:
    """Where a passage's rows go in its document: the passage's id, the rows
    it replaces (``None``: a new passage) and the first free numbers."""

    passage_id: UUID
    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    old: Passage | None
    next_index: int
    next_parent_index: int


def passage_rows(
    layout: PassageLayout,
    *,
    content: str,
    heading_path: Sequence[str],
    children: Sequence[PassageChild],
    revision: int,
    edited: bool,
    enabled: bool,
    disabled_reason: DisabledReason | None = None,
) -> list[KnowledgeChunk]:
    """The rows a passage's text is stored as — one rule for both stores.

    One chunk that is the whole text (same body and breadcrumb) stands for
    the passage, unless the passage already is a parent row (it stays one);
    otherwise a parent row with the passage's id holds the text and the
    chunks point at it. The chunks take the numbers of the ones they replace,
    in order — and their ids, under a parent row that stays — then the
    numbers after ``next_index``. Every row carries the passage's switch."""
    path = tuple(heading_path)
    old = layout.old
    stays_parent = old is not None and old.row.chunk_type is ChunkType.PARENT
    reason = None if enabled else disabled_reason

    def chunk(
        child: PassageChild, chunk_id: UUID, index: int, *, parent: UUID | None, rev: int
    ) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=chunk_id,
            tenant_id=layout.tenant_id,
            kb_id=layout.kb_id,
            document_id=layout.document_id,
            chunk_index=index,
            content=child.content,
            heading_path=child.heading_path,
            doc_title=child.doc_title,
            content_hash=child.content_hash,
            embedding=tuple(child.embedding),
            embedding_model=child.embedding_model,
            parent_chunk_id=parent,
            enabled=enabled,
            disabled_reason=reason,
            edited=edited,
            revision=rev,
        )

    alone = (
        len(children) == 1 and children[0].content == content and children[0].heading_path == path
    )
    if alone and not stays_parent:
        index = old.row.chunk_index if old is not None else layout.next_index
        return [chunk(children[0], layout.passage_id, index, parent=None, rev=revision)]
    rows = [
        KnowledgeChunk(
            id=layout.passage_id,
            tenant_id=layout.tenant_id,
            kb_id=layout.kb_id,
            document_id=layout.document_id,
            chunk_index=old.row.chunk_index
            if old is not None and stays_parent
            else layout.next_parent_index,
            content=content,
            heading_path=path,
            doc_title=children[0].doc_title if children else None,
            embedding=None,
            chunk_type=ChunkType.PARENT,
            enabled=enabled,
            disabled_reason=reason,
            edited=edited,
            revision=revision,
        )
    ]
    replaced = list(old.children) if old is not None else []
    extra = layout.next_index
    for position, child in enumerate(children):
        if position < len(replaced):
            index = replaced[position].chunk_index
            # A lone chunk's id moves to the parent row: its chunks get new ones.
            child_id = replaced[position].id if stays_parent else uuid4()
        else:
            index, child_id, extra = extra, uuid4(), extra + 1
        rows.append(chunk(child, child_id, index, parent=layout.passage_id, rev=1))
    return rows


def edited_since(
    original: PassageRevision | None, content: str, heading_path: Sequence[str]
) -> bool:
    """Whether a passage's text differs from ``original``, its first
    revision, when that is machine-made — always for one added by hand."""
    if original is None or original.edited_by is not None:
        return True
    return (original.content, original.heading_path) != (content, tuple(heading_path))


def unapplied_revisions(
    histories: Mapping[UUID, Sequence[PassageRevision]],
    current: Mapping[UUID, int],
    switch_offs: Sequence[PassageRevision] = (),
) -> list[PassageRevision]:
    """``list_unapplied_revisions`` from a document's text revision histories
    (each oldest first), its passages' current revisions and its switch-off
    records — one rule for both stores. A passage that is gone is listed when
    its text was edited; a switch-off record when no passage holds its text
    (``orphaned_at``)."""
    found: list[PassageRevision] = []
    for passage_id, history in histories.items():
        latest = history[-1]
        if passage_id in current:
            if current[passage_id] < latest.revision:
                found.append(latest)
        elif edited_since(history[0], latest.content, latest.heading_path):
            found.append(latest)
    found.extend(record for record in switch_offs if record.orphaned_at is not None)
    oldest = datetime.min.replace(tzinfo=UTC)
    found.sort(key=lambda r: (r.created_at or oldest, r.revision, str(r.passage_id)))
    return found


def paragraphs(text: str) -> list[str]:
    """A passage's paragraphs (blank-line separated), as the switch-off rules
    count them."""
    return [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]


def settle_switch_off(record: str, passage: str) -> str:
    """A switch-off record's text once a person has decided about a passage
    it hid (switched it on, or off as their own): without that passage's
    paragraphs — one rule for both stores. Empty: nothing left to hide."""
    settled = set(paragraphs(passage))
    return "\n\n".join(p for p in paragraphs(record) if p not in settled)


def manual_passages(
    rows: Sequence[KnowledgeChunk], histories: Mapping[UUID, Sequence[PassageRevision]]
) -> list[ManualPassage]:
    """``list_manual_passages`` from every passage row of a document in
    document order and the document's revision histories (each oldest first)
    — one rule for both stores. A passage is manual when it is edited,
    switched off by a person, past revision 1 or has a history — one
    switched off by inheritance only is not (that switch-off is derived anew
    from the switch-off records on every ingest); its ``occurrence`` counts
    the passages before it with the same machine text and breadcrumb, all of
    them."""
    passages = [
        ManualPassage(row, history[0] if (history := histories.get(row.id)) else None)
        for row in rows
    ]
    texts = Counter(p.anchor for p in passages if not p.added)
    seen: Counter[tuple[str | None, tuple[str, ...]]] = Counter()
    manual: list[ManualPassage] = []
    for passage in passages:
        row = passage.row
        key = (passage.anchor, passage.anchor_heading)
        occurrence = seen[key]
        seen[key] += 1
        switched_off = not row.enabled and row.disabled_reason != "inherited"
        if row.edited or switched_off or row.revision > 1 or passage.original is not None:
            unique = passage.added or texts[passage.anchor] == 1
            written = [r for r in histories.get(row.id, ()) if r.edited_by is not None]
            user_heading = written[-1].heading_path if written else None
            manual.append(ManualPassage(row, passage.original, occurrence, unique, user_heading))
    return manual


__all__ = [
    "SWITCH_OFF_REVISION",
    "ChunkVector",
    "DisabledReason",
    "ManualPassage",
    "Passage",
    "PassageChild",
    "PassageConflictError",
    "PassageLayout",
    "PassageRevision",
    "SwitchOffRecord",
    "edited_since",
    "manual_passages",
    "paragraphs",
    "passage_rows",
    "settle_switch_off",
    "unapplied_revisions",
]
