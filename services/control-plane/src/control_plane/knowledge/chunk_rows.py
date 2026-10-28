"""B-214 —— a document's chunks, built only when the store reads them.

The server's memory for an ingest follows the document's chunk count, not its
characters: every chunk carries a 1,024-float vector, about 32 KiB as Python
floats, and a document of short entries can have tens of thousands of chunks.
Vectors are kept here as ``array("d")`` (8 KiB, the same values) and a
:class:`~expert_work.protocol.KnowledgeChunk` is built when it is read —
``SqlKnowledgeStore.replace_chunks`` reads a batch at a time — so only one
batch of chunk objects exists at once.

The rows are the document's parent rows first (B-206 K-1: the passages a
search returns — no vector, their own ``chunk_index`` 0..m-1), then its chunks,
each pointing at its parent's row. A parent that is just its one chunk
(:attr:`~control_plane.knowledge.chunking.ParentChunk.is_trivial`) gets no row:
its chunk points at none and stands for itself.

A passage with an override (B-175 P3-PR2, a manual change put back) keeps its
id — on its parent row, or on its lone chunk — and its switch (and why it is
off), edit flag and revision; its chunks are switched with it.
"""

from __future__ import annotations

from array import array
from collections.abc import Mapping, Sequence
from typing import overload
from uuid import UUID, uuid4

from control_plane.knowledge.chunking import ParentChunk
from control_plane.knowledge.passages import PassageOverride
from expert_work.persistence.knowledge.passages import DisabledReason
from expert_work.protocol import ChunkType, KnowledgeChunk


def compact(vector: Sequence[float]) -> array[float]:
    """``vector`` as 8-byte floats in one buffer (no per-value objects); one
    that already is one is returned as is, not copied."""
    if isinstance(vector, array) and vector.typecode == "d":
        return vector
    return array("d", vector)


class ChunkRows(Sequence[KnowledgeChunk]):
    """The rows of one ingest — the parent rows, then the chunks in document
    order; each read builds it anew (same id every time). ``hashes`` are the
    chunks' (``parents``' children in order), ``vectors`` theirs by hash;
    ``overrides`` (when given, one per parent) what a passage keeps."""

    def __init__(
        self,
        *,
        parents: Sequence[ParentChunk],
        hashes: Sequence[str],
        vectors: Mapping[str, array[float]],
        tenant_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        doc_title: str | None,
        model_key: str | None,
        overrides: Sequence[PassageOverride | None] = (),
    ) -> None:
        self._parents: list[ParentChunk] = []  # the parents that get a row
        self._pieces = [child for parent in parents for child in parent.children]
        self._owners: list[int | None] = []  # each chunk's parent row, if any
        self._parent_marks: list[PassageOverride | None] = []
        self._marks: list[PassageOverride | None] = []  # each chunk's passage's override
        for parent, mark in zip(parents, overrides or [None] * len(parents), strict=True):
            owner = None if parent.is_trivial else len(self._parents)
            if owner is not None:
                self._parents.append(parent)
                self._parent_marks.append(mark)
            self._owners.extend([owner] * len(parent.children))
            self._marks.extend([mark] * len(parent.children))
        self._hashes = hashes
        self._vectors = vectors
        self._parent_ids = [_kept_id(mark) for mark in self._parent_marks]
        # A lone chunk is its passage: it takes the passage's id.
        self._ids = [
            _kept_id(mark) if owner is None else uuid4()
            for owner, mark in zip(self._owners, self._marks, strict=True)
        ]
        self._tenant_id = tenant_id
        self._kb_id = kb_id
        self._document_id = document_id
        self._doc_title = doc_title
        self._model_key = model_key

    def __len__(self) -> int:
        return len(self._parents) + len(self._pieces)

    @overload
    def __getitem__(self, index: int) -> KnowledgeChunk: ...

    @overload
    def __getitem__(self, index: slice) -> list[KnowledgeChunk]: ...

    def __getitem__(self, index: int | slice) -> KnowledgeChunk | list[KnowledgeChunk]:
        if isinstance(index, slice):
            return [self._chunk(i) for i in range(*index.indices(len(self)))]
        if not -len(self) <= index < len(self):
            raise IndexError(index)
        return self._chunk(index % len(self))

    def _chunk(self, index: int) -> KnowledgeChunk:
        if index < len(self._parents):
            return self._parent_row(index)
        index -= len(self._parents)
        piece, digest, owner = self._pieces[index], self._hashes[index], self._owners[index]
        mark = self._marks[index]
        return KnowledgeChunk(
            id=self._ids[index],
            tenant_id=self._tenant_id,
            kb_id=self._kb_id,
            document_id=self._document_id,
            chunk_index=index,
            content=piece.body,
            heading_path=piece.heading_path,
            doc_title=self._doc_title,
            content_hash=digest,
            embedding=tuple(self._vectors[digest]),
            embedding_model=self._model_key,
            parent_chunk_id=None if owner is None else self._parent_ids[owner],
            enabled=mark is None or mark.enabled,
            disabled_reason=_reason(mark),
            edited=mark is not None and mark.edited,
            # The revision is the passage's: on its parent row when it has one.
            revision=mark.revision if mark is not None and owner is None else 1,
        )

    def _parent_row(self, index: int) -> KnowledgeChunk:
        # Never embedded: no vector, no model and no hash, so a re-ingest's
        # vector reuse can never match it.
        parent, mark = self._parents[index], self._parent_marks[index]
        return KnowledgeChunk(
            id=self._parent_ids[index],
            tenant_id=self._tenant_id,
            kb_id=self._kb_id,
            document_id=self._document_id,
            chunk_index=index,
            content=parent.body,
            heading_path=parent.heading_path,
            doc_title=self._doc_title,
            embedding=None,
            chunk_type=ChunkType.PARENT,
            enabled=mark is None or mark.enabled,
            disabled_reason=_reason(mark),
            edited=mark is not None and mark.edited,
            revision=1 if mark is None else mark.revision,
        )


def _kept_id(mark: PassageOverride | None) -> UUID:
    return mark.id if mark is not None and mark.id is not None else uuid4()


def _reason(mark: PassageOverride | None) -> DisabledReason | None:
    if mark is None or mark.enabled:
        return None
    return mark.disabled_reason or "manual"
