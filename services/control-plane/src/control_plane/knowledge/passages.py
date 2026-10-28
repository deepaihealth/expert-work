"""B-175 P3-PR2 —— a re-ingest carries a document's manual changes over.

Every ingest replaces all of a document's chunks. Before the new chunks are
written, :func:`replay_manual_changes` puts back what people changed on the
old ones (``KnowledgeStore.list_manual_passages``), finding each passage among
the new parents by the machine text **and breadcrumb** it was cut as (its first
revision; its own text and breadcrumb when it has no history). Identical
copies (a template repeated under the same heading) are told apart by order:
the k-th old passage with that text and breadcrumb is the k-th new one.

* found — the passage keeps its id, switch (and why it is off) and revision;
  an edited one gets its edited text back, cut into chunks by the current
  parameters (:func:`~control_plane.knowledge.chunking.chunk_passage_body`) —
  the same chunks as before when the parameters are the same, so nothing is
  embedded again;
* found only by its text, under another breadcrumb (the file renamed the
  section) — only when no other passage, old or new, has that text: put back
  under the new breadcrumb (or the one the user chose, if they changed it:
  their latest revision's breadcrumb differs from revision 1's);
* not found — an edit is not applied; its revisions stay, and
  ``list_unapplied_revisions`` lists it. A passage a person switched off
  becomes a **switch-off record** (its machine text and breadcrumb), kept by
  the store (``KnowledgeStore.list_switch_offs``);
* added by hand — always back, after the file's passages, in their order.

Inherited switch-offs are derived, never a source: on every ingest the new
parents holding any distinctive paragraph (long, and found exactly once in the
new document) of any switch-off record — the document's earlier ones and those
this ingest makes — are switched off and marked
(``disabled_reason='inherited'``) for review; the product rule (10-27) is to
err on hiding. A passage off by inheritance only is no manual passage: it is
not carried over, so what one ingest hid is worked out again from the records
on the next, and moving boundaries cannot make it spread. A record no new
parent holds text of switches nothing off and is listed for review. A person
settling an inherited passage (switched on, or off as their own) takes its
paragraphs out of the records (``KnowledgeStore.set_passage_enabled``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from control_plane.knowledge.chunking import ParentChunk, chunk_passage_body
from expert_work.persistence.knowledge.passages import (
    DisabledReason,
    ManualPassage,
    PassageRevision,
    SwitchOffRecord,
    paragraphs,
)

#: A paragraph this long can carry a switch-off over to a new parent; shorter
#: ones (a heading line, a "see below") say nothing about which passage it is.
_SHARED_PARAGRAPH_MIN_CHARS = 20

_Key = tuple[str | None, tuple[str, ...]]


@dataclass(frozen=True)
class PassageOverride:
    """What an ingest writes for one passage instead of a new one's defaults:
    its id (``None``: a new id), switch and why it is off, edit flag and
    revision."""

    id: UUID | None
    enabled: bool = True
    edited: bool = False
    revision: int = 1
    disabled_reason: DisabledReason | None = None

    @property
    def inherited(self) -> bool:
        return self.disabled_reason == "inherited"


@dataclass(frozen=True)
class PassagePlan:
    """The passages an ingest writes, in order, each with its override."""

    parents: list[ParentChunk]
    overrides: list[PassageOverride | None]
    edits_applied: int = 0
    disabled_applied: int = 0
    added: int = 0
    disabled_inherited: int = 0
    #: Edited passages that could not be found: their edits are not applied.
    unapplied: tuple[UUID, ...] = ()
    #: Every switch-off record the inherited switch-offs came from (the
    #: document's, and new ones for switched-off passages not found), each
    #: saying whether a new parent holds its text — what the store writes.
    switch_offs: tuple[SwitchOffRecord, ...] = ()

    @property
    def chunk_count(self) -> int:
        return sum(len(parent.children) for parent in self.parents)

    @property
    def switch_offs_unplaced(self) -> int:
        """Records no new parent holds text of (listed for review)."""
        return sum(not record.placed for record in self.switch_offs)


class _Replay:
    """One replay's state: the new parents, which are claimed, the counts."""

    def __init__(
        self,
        parents: Sequence[ParentChunk],
        *,
        max_tokens: int,
        overlap_tokens: int,
        doc_title: str | None,
    ) -> None:
        self.parents = parents
        self.out = list(parents)
        self.overrides: list[PassageOverride | None] = [None] * len(parents)
        self.claimed: set[int] = set()
        self.edits = self.disabled = 0
        self.unapplied: list[UUID] = []
        #: Switch-off records — the document's, and ``new`` ones for misses
        #: (``placed`` is worked out by :meth:`inherit`).
        self.sources: list[SwitchOffRecord] = []
        self._max_tokens = max_tokens
        self._overlap_tokens = overlap_tokens
        self._doc_title = doc_title
        self._slots: dict[_Key, list[int]] = {}
        for index, parent in enumerate(parents):
            self._slots.setdefault((parent.body, parent.heading_path), []).append(index)
        self._texts = Counter(parent.body for parent in parents)

    def recut(self, content: str, heading_path: tuple[str, ...]) -> ParentChunk:
        children = chunk_passage_body(
            content,
            heading_path,
            max_tokens=self._max_tokens,
            overlap_tokens=self._overlap_tokens,
            doc_title=self._doc_title,
        )
        return ParentChunk(body=content, heading_path=heading_path, children=tuple(children))

    def by_key(self, passage: ManualPassage) -> int | None:
        """The new parent that is this passage: the same text and breadcrumb,
        the same place among identical copies."""
        slots = self._slots.get((passage.anchor, passage.anchor_heading), [])
        if passage.occurrence < len(slots) and slots[passage.occurrence] not in self.claimed:
            return slots[passage.occurrence]
        return None

    def by_text(self, passage: ManualPassage) -> int | None:
        """The new parent with this passage's text under another breadcrumb —
        only when neither document has that text twice."""
        anchor = passage.anchor
        if anchor is None or not passage.unique_text or self._texts[anchor] != 1:
            return None
        index = next(i for i, parent in enumerate(self.parents) if parent.body == anchor)
        return None if index in self.claimed else index

    def claim(self, passage: ManualPassage, index: int) -> None:
        row = passage.row
        self.claimed.add(index)
        if row.edited:
            # The user's breadcrumb when they changed it (their latest revision
            # says other than revision 1); else the file's. Not the row's: an
            # earlier re-ingest may have moved it there.
            user = passage.user_heading
            heading = (
                self.parents[index].heading_path
                if user is None or user == passage.anchor_heading
                else user
            )
            self.out[index] = self.recut(row.content, heading)
            self.edits += 1
        # Off by inheritance is not carried over: it is derived again below.
        off = _switched_off(passage)
        self.disabled += off
        self.overrides[index] = PassageOverride(
            id=row.id,
            enabled=not off,
            edited=row.edited,
            revision=row.revision,
            disabled_reason="manual" if off else None,
        )

    def miss(self, passage: ManualPassage) -> None:
        row = passage.row
        if row.edited:
            self.unapplied.append(row.id)
        if _switched_off(passage) and passage.anchor is not None:
            self.sources.append(
                SwitchOffRecord(
                    row.id, passage.anchor, passage.anchor_heading, placed=False, new=True
                )
            )

    def inherit(self) -> tuple[int, list[SwitchOffRecord]]:
        """Switch off every new parent holding any distinctive paragraph of a
        switch-off record; returns how many were switched off, and each
        record with whether a new parent holds its text."""
        split = [paragraphs(parent.body) for parent in self.parents]  # once per replay
        counts = Counter(p for ps in split for p in ps)
        owned = [set(ps) for ps in split]
        inherited = 0
        records: list[SwitchOffRecord] = []
        for source in self.sources:
            distinctive = {
                p
                for p in paragraphs(source.content)
                if len(p) >= _SHARED_PARAGRAPH_MIN_CHARS and counts[p] == 1
            }
            holders = [index for index, own in enumerate(owned) if distinctive & own]
            records.append(replace(source, placed=bool(holders)))
            for index in holders:
                current = self.overrides[index]
                if current is not None and not current.enabled:
                    continue  # already off (its own switch, or another record's)
                base = current or PassageOverride(id=None)
                self.overrides[index] = replace(base, enabled=False, disabled_reason="inherited")
                inherited += 1
        return inherited, records


def replay_manual_changes(
    parents: Sequence[ParentChunk],
    manual: Sequence[ManualPassage],
    *,
    switch_offs: Sequence[PassageRevision] = (),
    max_tokens: int,
    overlap_tokens: int,
    doc_title: str | None,
) -> PassagePlan:
    """The new ``parents`` with ``manual`` put back and the document's
    ``switch_offs`` records applied (see the module doc)."""
    replay = _Replay(
        parents, max_tokens=max_tokens, overlap_tokens=overlap_tokens, doc_title=doc_title
    )
    replay.sources = [
        SwitchOffRecord(r.passage_id, r.content, r.heading_path, placed=False) for r in switch_offs
    ]
    from_file = [passage for passage in manual if not passage.added]
    by_text: list[ManualPassage] = []
    for passage in from_file:
        index = replay.by_key(passage)
        if index is None:
            by_text.append(passage)
        else:
            replay.claim(passage, index)
    for passage in by_text:
        index = replay.by_text(passage)
        if index is None:
            replay.miss(passage)
        else:
            replay.claim(passage, index)
    inherited, records = replay.inherit()

    added = [passage for passage in manual if passage.added]
    for passage in added:
        row = passage.row
        replay.out.append(replay.recut(row.content, row.heading_path))
        replay.overrides.append(
            PassageOverride(
                id=row.id,
                enabled=row.enabled,
                edited=True,
                revision=row.revision,
                disabled_reason=None if row.enabled else (row.disabled_reason or "manual"),
            )
        )
    return PassagePlan(
        parents=replay.out,
        overrides=replay.overrides,
        edits_applied=replay.edits,
        disabled_applied=replay.disabled,
        added=len(added),
        disabled_inherited=inherited,
        unapplied=tuple(replay.unapplied),
        switch_offs=tuple(records),
    )


def _switched_off(passage: ManualPassage) -> bool:
    """Switched off by a person (an inherited switch-off is derived, not theirs)."""
    row = passage.row
    return not row.enabled and row.disabled_reason != "inherited"
