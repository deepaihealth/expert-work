"""B-215 —— a re-ingest does not pay again for topic detection.

The semantic chunker embeds every block of a document to find topic shifts;
the chunks' vectors were reused by content hash but those block embeddings
were not, so every re-ingest / 「补识别」 paid for them again (10-23 test: a
2.5M-character PDF re-recognized with nothing new read was billed 683,103
embedding tokens). Decisions are now kept per document and embedding model in
the recognition cache (``break_cache``), and only pairs not seen before are
embedded.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge import break_cache
from control_plane.knowledge.chunking import BreakMemo, chunk_markdown_semantic
from control_plane.knowledge.ingestion import ingest_markdown
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import DEFAULT_CHUNK_MAX_TOKENS, DEFAULT_CHUNK_OVERLAP_TOKENS

_TENANT = uuid4()


class _TopicEmbedder:
    """``[1, 0]`` for an alpha text, ``[0, 1]`` otherwise; a fixed model;
    records every text it embeds."""

    def __init__(self, model_key: str = "fake/topic") -> None:
        self.model_key = model_key
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        self.texts.extend(texts)
        return [(1.0, 0.0) if "alpha" in text else (0.0, 1.0) for text in texts]

    def blocks(self) -> list[str]:
        """Texts embedded for topic detection (chunk texts carry the title)."""
        return [text for text in self.texts if not text.startswith("[Document: ")]


def _paragraphs(topics: str) -> list[str]:
    tail = "a paragraph long enough to be a chunk on its own " + "word " * 30
    return [f"{'alpha' if t == 'a' else 'beta'} {i}: {tail}" for i, t in enumerate(topics)]


async def _ingest(
    store: InMemoryKnowledgeStore, doc: tuple[UUID, UUID], paragraphs: list[str], embedder: Any
) -> list[str]:
    kb_id, doc_id = doc
    await ingest_markdown(
        store=store,
        embedder=embedder,
        tenant_id=_TENANT,
        document_id=doc_id,
        kb_id=kb_id,
        markdown="\n\n".join(paragraphs),
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        doc_title="report",
    )
    chunks, _ = await store.list_chunks(tenant_id=_TENANT, document_id=doc_id, limit=200)
    return [chunk.content for chunk in chunks]


async def _document(store: InMemoryKnowledgeStore) -> tuple[UUID, UUID]:
    base = await store.create_base(tenant_id=_TENANT, name=f"kb-{uuid4().hex[:6]}")
    doc = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="report.md", content=b"x"
    )
    return base.id, doc.id


async def test_a_re_ingest_of_the_same_text_embeds_no_block() -> None:
    store = InMemoryKnowledgeStore()
    doc = await _document(store)
    paragraphs = _paragraphs("aabbbaab")
    first = _TopicEmbedder()
    chunks = await _ingest(store, doc, paragraphs, first)
    assert len(first.blocks()) == len(paragraphs)
    again = _TopicEmbedder()
    assert await _ingest(store, doc, paragraphs, again) == chunks
    assert again.blocks() == []
    assert again.texts == []  # the chunks' own vectors were reused already


async def test_an_edit_embeds_only_the_blocks_next_to_it() -> None:
    store = InMemoryKnowledgeStore()
    doc = await _document(store)
    paragraphs = _paragraphs("aaaabbbbaaaa")
    await _ingest(store, doc, paragraphs, _TopicEmbedder())
    edited = list(paragraphs)
    edited[6] = edited[6].replace("beta 6:", "beta 6 (revised):")
    embedder = _TopicEmbedder()
    chunks = await _ingest(store, doc, edited, embedder)
    # Pairs (5, 6) and (6, 7) are new: blocks 5, 6 and 7.
    assert embedder.blocks() == [edited[5].strip(), edited[6].strip(), edited[7].strip()]
    fresh_store = InMemoryKnowledgeStore()
    fresh = _TopicEmbedder()
    assert chunks == await _ingest(fresh_store, await _document(fresh_store), edited, fresh)


async def test_another_embedding_model_decides_afresh() -> None:
    store = InMemoryKnowledgeStore()
    doc = await _document(store)
    paragraphs = _paragraphs("aabb")
    await _ingest(store, doc, paragraphs, _TopicEmbedder("fake/topic"))
    other = _TopicEmbedder("fake/other")
    await _ingest(store, doc, paragraphs, other)
    assert len(other.blocks()) == len(paragraphs)


async def test_no_known_model_keeps_no_memo() -> None:
    class _Anonymous(_TopicEmbedder):
        def __init__(self) -> None:
            super().__init__()
            del self.model_key

    store = InMemoryKnowledgeStore()
    doc = await _document(store)
    paragraphs = _paragraphs("ab")
    await _ingest(store, doc, paragraphs, _Anonymous())
    again = _Anonymous()
    await _ingest(store, doc, paragraphs, again)
    assert len(again.blocks()) == len(paragraphs)


async def test_a_cache_that_cannot_be_read_only_costs_the_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryKnowledgeStore()
    doc = await _document(store)
    paragraphs = _paragraphs("aabb")
    chunks = await _ingest(store, doc, paragraphs, _TopicEmbedder())

    async def broken(**_: object) -> dict[str, object]:
        raise RuntimeError("cache down")

    monkeypatch.setattr(store, "get_parse_cache", broken)
    embedder = _TopicEmbedder()
    assert await _ingest(store, doc, paragraphs, embedder) == chunks
    assert len(embedder.blocks()) == len(paragraphs)


async def test_the_memo_keeps_only_the_pairs_the_text_has_now() -> None:
    store = InMemoryKnowledgeStore()
    doc = await _document(store)
    await _ingest(store, doc, _paragraphs("aabbaa"), _TopicEmbedder())
    await _ingest(store, doc, _paragraphs("aab"), _TopicEmbedder())
    memo = await break_cache.load_break_memo(
        store, tenant_id=_TENANT, document_id=doc[1], model_key="fake/topic"
    )
    assert memo is not None and len(memo.known) == 2  # pairs (0,1) and (1,2) only


async def test_the_chunks_with_a_memo_are_those_without_one() -> None:
    markdown = "\n\n".join(_paragraphs("abba" * 6))
    kwargs: dict[str, Any] = {"max_tokens": 512, "overlap_tokens": 0, "tenant_id": _TENANT}
    plain = await chunk_markdown_semantic(markdown, embedder=_TopicEmbedder(), **kwargs)
    memo = BreakMemo(namespace="fake/topic")
    first = await chunk_markdown_semantic(markdown, embedder=_TopicEmbedder(), memo=memo, **kwargs)
    reused = BreakMemo(namespace="fake/topic", known=dict(memo.decided))
    embedder = _TopicEmbedder()
    second = await chunk_markdown_semantic(markdown, embedder=embedder, memo=reused, **kwargs)
    assert plain == first == second
    assert embedder.texts == []


def test_a_decision_is_keyed_by_both_texts_the_model_and_nothing_else() -> None:
    memo = BreakMemo(namespace="m1")
    assert memo.key("a", "b") == memo.key("a", "b")
    assert len({memo.key("a", "b"), memo.key("b", "a"), memo.key("a", "c")}) == 3
    assert memo.key("a", "b") != BreakMemo(namespace="m2").key("a", "b")
    assert memo.key("a\x00b", "c") != memo.key("a", "b\x00c")
