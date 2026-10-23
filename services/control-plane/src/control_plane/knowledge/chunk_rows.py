"""B-214 —— a document's chunks, built only when the store reads them.

The server's memory for an ingest follows the document's chunk count, not its
characters: every chunk carries a 1,024-float vector, about 32 KiB as Python
floats, and a document of short entries can have tens of thousands of chunks.
Vectors are kept here as ``array("d")`` (8 KiB, the same values) and a
:class:`~expert_work.protocol.KnowledgeChunk` is built when it is read —
``SqlKnowledgeStore.replace_chunks`` reads a batch at a time — so only one
batch of chunk objects exists at once.
"""

from __future__ import annotations

from array import array
from collections.abc import Mapping, Sequence
from typing import overload
from uuid import UUID, uuid4

from control_plane.knowledge.chunking import TextChunk
from expert_work.protocol import KnowledgeChunk


def compact(vector: Sequence[float]) -> array[float]:
    """``vector`` as 8-byte floats in one buffer (no per-value objects)."""
    return array("d", vector)


class ChunkRows(Sequence[KnowledgeChunk]):
    """The chunks of one ingest in document order; each read builds it anew
    (same id every time)."""

    def __init__(
        self,
        *,
        pieces: Sequence[TextChunk],
        hashes: Sequence[str],
        vectors: Mapping[str, array[float]],
        tenant_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        doc_title: str | None,
        model_key: str | None,
    ) -> None:
        self._pieces = pieces
        self._hashes = hashes
        self._vectors = vectors
        self._ids = [uuid4() for _ in pieces]
        self._tenant_id = tenant_id
        self._kb_id = kb_id
        self._document_id = document_id
        self._doc_title = doc_title
        self._model_key = model_key

    def __len__(self) -> int:
        return len(self._pieces)

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
        piece, digest = self._pieces[index], self._hashes[index]
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
        )
