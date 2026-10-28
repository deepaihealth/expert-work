"""Copies among a knowledge search's passages (B-206 K-21②).

The same text uploaded under two file names is two documents, so its passages
come back twice, each copy taking a rerank slot and a result another passage
could have had. A passage's key is its section breadcrumb and body, NFC-normalised
with whitespace collapsed, and hashed: only exact copies match (no case folding or
NFKC, which would merge identifiers or turn a superscript 5 into a 5). The document
title is left out: it is what differs between the copies (a chunk's
``content_hash`` covers its retrieval text, title included, so it cannot find
them). The same body under another section is another passage.

Results are parents (B-206 K-1), so a copy is a *passage* that repeats: before the
rerank a child is dropped only when its own text repeats an earlier child's and
the passage it shows (its parent, or itself without one) repeats that child's
passage too — two versions of a document sharing a clause word for word keep both
passages (design §7: conflicting versions are shown, not resolved). Ranked lists
keep the first, best-ranked, passage of each key, as WeKnora does before and after
its rerank (``searchutil/textutil.go``).
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import Counter
from collections.abc import Hashable, Mapping, Sequence
from uuid import UUID

from expert_work.protocol import KnowledgeChunk, chunk_retrieval_text


def passage_key(heading_path: Sequence[str], content: str) -> bytes:
    """The key two copies of a passage share: its breadcrumb + body, NFC with
    whitespace collapsed, hashed."""
    text = unicodedata.normalize("NFC", chunk_retrieval_text(heading_path, content))
    return hashlib.blake2b(" ".join(text.split()).encode("utf-8"), digest_size=16).digest()


def passage_of(chunk: KnowledgeChunk) -> UUID:
    """The passage a ranked child stands for in the results: its parent row's
    id, or its own when it has none (the grouping of
    :func:`~orchestrator.tools.knowledge_results.collapse_to_parents`)."""
    return chunk.parent_chunk_id or chunk.id


def _own_key(chunk: KnowledgeChunk, scopes: Mapping[UUID, Hashable]) -> tuple[Hashable, bytes]:
    return scopes[chunk.kb_id], passage_key(chunk.heading_path, chunk.content)


def repeated_children(
    chunks: Sequence[KnowledgeChunk], scopes: Mapping[UUID, Hashable]
) -> list[KnowledgeChunk]:
    """The ``chunks`` whose own text repeats another's in the same scope
    (``scopes`` by base id) — the only ones whose parent row has to be read to
    tell whether they show a copy."""
    keys = [_own_key(chunk, scopes) for chunk in chunks]
    counts = Counter(keys)
    return [chunk for chunk, key in zip(chunks, keys, strict=True) if counts[key] > 1]


def drop_copied_children(
    chunks: Sequence[KnowledgeChunk],
    scopes: Mapping[UUID, Hashable],
    parents: Mapping[UUID, KnowledgeChunk],
) -> tuple[list[KnowledgeChunk], frozenset[UUID]]:
    """``chunks`` (ranked best first) without a child whose own text and shown
    passage (its row in ``parents``, else the child itself) both repeat an
    earlier kept child's in the same scope; and the passages (:func:`passage_of`)
    that thereby have no child left."""
    seen: dict[tuple[Hashable, bytes], set[bytes]] = {}
    kept: list[KnowledgeChunk] = []
    dropped: set[UUID] = set()
    for chunk in chunks:
        own = _own_key(chunk, scopes)
        pid = chunk.parent_chunk_id
        parent = parents.get(pid) if pid is not None else None
        shown = parent if parent is not None else chunk
        shown_key = passage_key(shown.heading_path, shown.content)
        if shown_key in seen.get(own, set()):
            dropped.add(passage_of(chunk))
            continue
        seen.setdefault(own, set()).add(shown_key)
        kept.append(chunk)
    return kept, frozenset(dropped - {passage_of(chunk) for chunk in kept})
