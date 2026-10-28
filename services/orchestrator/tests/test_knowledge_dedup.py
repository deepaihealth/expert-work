"""B-206 K-21② — a knowledge search drops copies of a better-ranked passage.

The same text uploaded under two file names is two documents; without this its
passages come back twice, each taking a rerank slot and a result. Copies are
found by the passage's breadcrumb + body, normalised — never by the document
title, which is what differs between them — and the best-ranked copy stays.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import ChunkType, KnowledgeChunk, RetrievalMethod
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools.knowledge import KnowledgeSearchOutcome
from orchestrator.tools.knowledge_dedup import (
    drop_copied_children,
    passage_key,
    repeated_children,
)

_BODY = "The deductible is 500 dollars."

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Embedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0, 0.0) for _ in texts]


class _RecordingReranker:
    """A scoring reranker that keeps the order it is handed (scores 1, 0.99, …)
    and records the documents of every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        ranked = await self.rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )
        return [index for index, _score in ranked]

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        self.calls.append(list(documents))
        return [(i, 1.0 - i / 100) for i in range(min(top_k, len(documents)))]


@dataclass(frozen=True)
class _Passage:
    """One child chunk: ``rank`` orders vector recall (0 = closest to the query)."""

    content: str
    rank: int
    heading_path: tuple[str, ...] = ("H",)


async def _base(files: dict[str, Sequence[_Passage]]) -> tuple[InMemoryKnowledgeStore, UUID]:
    """Base ``kb`` with one document per file name, each chunk titled after
    its file as ingestion titles them."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    for filename, passages in files.items():
        document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=filename)
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=document.id,
            chunks=[
                KnowledgeChunk(
                    id=uuid4(),
                    tenant_id=tenant,
                    kb_id=base.id,
                    document_id=document.id,
                    chunk_index=index,
                    content=passage.content,
                    heading_path=passage.heading_path,
                    doc_title=filename.removesuffix(".md"),
                    embedding=(1.0, passage.rank / 10),
                )
                for index, passage in enumerate(passages)
            ],
        )
    return store, tenant


async def _search(
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    *,
    reranker: _RecordingReranker | None = None,
    dedup: bool = True,
    limit: int = 5,
    recall_limit: int | None = None,
) -> KnowledgeSearchOutcome:
    retriever = KnowledgeRetriever(
        store=store,
        embedder=_Embedder(),
        reranker=reranker,
        dedup=dedup,
        recall_limit=recall_limit,
    )
    return await retriever.search_detailed(
        tenant_id=tenant,
        base_names=["kb"],
        query="deductible",
        limit=limit,
        method=RetrievalMethod.VECTOR,
    )


def _copies(documents: Sequence[str]) -> int:
    return sum(_BODY in document for document in documents)


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_whitespace_and_composition_differences_are_the_same_passage() -> None:
    assert passage_key(("Fees",), "The  deductible\n is 500\tdollars.") == passage_key(
        ("Fees",), "The deductible is 500 dollars."
    )
    # The ideographic space is whitespace; NFC: a precomposed letter and its
    # decomposed form are one text.
    assert passage_key((), "a" + chr(0x3000) + "b") == passage_key((), "a b")
    assert passage_key((), "caf" + chr(0xE9)) == passage_key((), "cafe" + chr(0x301))


def test_case_and_width_differences_are_different_passages() -> None:
    """Only exact copies: case can tell identifiers apart, and NFKC would fold a
    superscript (10 to the 5th) into a different number (105)."""
    assert passage_key((), "Code QX-7a") != passage_key((), "Code qx-7A")
    assert passage_key((), "<=10" + chr(0x2075) + " CFU/g") != passage_key((), "<=105 CFU/g")
    wide = "".join(map(chr, (0xFF21, 0xFF22, 0xFF23)))
    assert passage_key((), wide) != passage_key((), "ABC")


def test_a_different_section_or_body_is_a_different_passage() -> None:
    assert passage_key(("Adults",), "Two tablets.") != passage_key(("Children",), "Two tablets.")
    assert passage_key(("H",), "Two tablets.") != passage_key(("H",), "Three tablets.")


_KB = uuid4()


def _chunk(content: str, *, parent: KnowledgeChunk | None = None, kb: UUID = _KB) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=kb,
        document_id=uuid4(),
        chunk_index=0,
        content=content,
        embedding=None,
        parent_chunk_id=None if parent is None else parent.id,
    )


def test_a_child_is_dropped_only_when_the_passage_it_shows_is_a_copy_too() -> None:
    """Two versions of a policy share clause 3 word for word: the newer version's
    clause-3 child is not a copy, the passage it shows differs (design §7: never
    hide a conflicting version). A third, identical document's is."""
    old = _chunk("Clause 3. Clause 4: 500.")
    new = _chunk("Clause 3. Clause 4: 800.")
    same = _chunk("Clause 3. Clause 4: 500.")
    first = _chunk("Clause 3.", parent=old)
    other_version = _chunk("Clause 3.", parent=new)
    copy = _chunk("Clause 3.", parent=same)
    chunks = [first, other_version, copy]
    assert repeated_children(chunks, {_KB: None}) == chunks
    parents = {p.id: p for p in (old, new, same)}
    kept, dropped = drop_copied_children(chunks, {_KB: None}, parents)
    assert kept == [first, other_version]
    assert dropped == frozenset({same.id})


def test_only_children_whose_own_text_repeats_are_suspects() -> None:
    one, two, three = _chunk("a"), _chunk("b"), _chunk("a")
    assert repeated_children([one, two, three], {_KB: None}) == [one, three]


def test_a_repeated_child_of_the_same_passage_is_not_a_dropped_passage() -> None:
    parent = _chunk("a. a.")
    first, again = _chunk("a.", parent=parent), _chunk("a.", parent=parent)
    kept, dropped = drop_copied_children([first, again], {_KB: None}, {parent.id: parent})
    assert kept == [first]
    assert dropped == frozenset()


def test_copies_in_scopes_with_different_floors_are_both_kept() -> None:
    strict, lenient = uuid4(), uuid4()
    one, two = _chunk("a", kb=strict), _chunk("a", kb=lenient)
    scopes = {strict: 0.6, lenient: None}
    assert repeated_children([one, two], scopes) == []
    assert drop_copied_children([one, two], scopes, {}) == ([one, two], frozenset())


# ---------------------------------------------------------------------------
# Before the rerank (children)
# ---------------------------------------------------------------------------


async def test_the_reranker_judges_one_copy_of_a_passage_uploaded_twice() -> None:
    store, tenant = await _base(
        {
            "a.md": [_Passage(_BODY, rank=1)],
            "b.md": [_Passage(_BODY, rank=2)],
            "c.md": [_Passage("Vacation is 20 days.", rank=3)],
        }
    )
    reranker = _RecordingReranker()
    outcome = await _search(store, tenant, reranker=reranker)
    titled = reranker.calls[0]
    assert (_copies(titled), len(titled)) == (1, 2)
    assert outcome.duplicates_dropped == 1
    assert [r.content for r in outcome.results] == [_BODY, "Vacation is 20 days."]


async def test_the_copy_ranked_higher_by_fusion_is_the_one_kept() -> None:
    store, tenant = await _base(
        {"a.md": [_Passage(_BODY, rank=2)], "b.md": [_Passage(_BODY, rank=1)]}
    )
    outcome = await _search(store, tenant)
    assert [r.filename for r in outcome.results] == ["b.md"]


async def test_the_same_body_under_another_section_is_kept() -> None:
    store, tenant = await _base(
        {
            "a.md": [_Passage(_BODY, rank=1, heading_path=("Plan A",))],
            "b.md": [_Passage(_BODY, rank=2, heading_path=("Plan B",))],
        }
    )
    outcome = await _search(store, tenant)
    assert [r.filename for r in outcome.results] == ["a.md", "b.md"]
    assert outcome.duplicates_dropped == 0


async def test_dedup_off_keeps_every_copy() -> None:
    """The eval's A/B arm: today's behaviour, copies and all."""
    store, tenant = await _base(
        {"a.md": [_Passage(_BODY, rank=1)], "b.md": [_Passage(_BODY, rank=2)]}
    )
    reranker = _RecordingReranker()
    outcome = await _search(store, tenant, reranker=reranker, dedup=False)
    assert _copies(reranker.calls[0]) == 2
    assert [r.filename for r in outcome.results] == ["a.md", "b.md"]
    assert outcome.duplicates_dropped == 0


async def test_a_search_without_rerank_drops_copies_too() -> None:
    store, tenant = await _base(
        {"a.md": [_Passage(_BODY, rank=1)], "b.md": [_Passage(_BODY, rank=2)]}
    )
    outcome = await _search(store, tenant, reranker=None)
    assert [r.filename for r in outcome.results] == ["a.md"]
    assert outcome.duplicates_dropped == 1


# ---------------------------------------------------------------------------
# After the rerank (parents)
# ---------------------------------------------------------------------------


class _ReadRecordingStore(InMemoryKnowledgeStore):
    """Records every ``get_chunks`` call's ids."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[list[UUID]] = []

    async def get_chunks(
        self, *, tenant_id: UUID, chunk_ids: Sequence[UUID]
    ) -> list[KnowledgeChunk]:
        self.reads.append(list(chunk_ids))
        return await super().get_chunks(tenant_id=tenant_id, chunk_ids=chunk_ids)


#: A document as ``(parent bodies, children)``; a child is ``(index of its
#: parent or None for no parent row, body, vector rank)``.
_Doc = tuple[Sequence[str], Sequence[tuple[int | None, str, int]]]


async def _parented(files: dict[str, _Doc]) -> tuple[_ReadRecordingStore, UUID]:
    store = _ReadRecordingStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    for filename, (parent_bodies, children) in files.items():
        document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=filename)
        parents = [
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=base.id,
                document_id=document.id,
                chunk_index=i,
                content=body,
                heading_path=("H",),
                chunk_type=ChunkType.PARENT,
                embedding=None,
            )
            for i, body in enumerate(parent_bodies)
        ]
        kids = [
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=base.id,
                document_id=document.id,
                chunk_index=i,
                content=body,
                heading_path=("H",),
                parent_chunk_id=None if ref is None else parents[ref].id,
                embedding=(1.0, rank / 10),
            )
            for i, (ref, body, rank) in enumerate(children)
        ]
        await store.replace_chunks(
            tenant_id=tenant, document_id=document.id, chunks=[*parents, *kids]
        )
    return store, tenant


#: Two documents sharing a section, cut into different children (so no child is
#: a copy of another), and two more documents.
_SHARED_SECTION: dict[str, _Doc] = {
    "a.md": (["SHARED SECTION"], [(0, "SHARED", 1), (0, "SECTION", 3)]),
    "b.md": (["SHARED SECTION"], [(0, "SHARED SECTION", 2)]),
    "c.md": (["OTHER"], [(0, "OTHER", 4)]),
    "d.md": (["LAST"], [(0, "LAST", 5)]),
}


async def test_a_section_shared_by_two_documents_shows_once_and_limit_is_still_filled() -> None:
    store, tenant = await _parented(_SHARED_SECTION)
    outcome = await _search(store, tenant, limit=2)
    assert [(r.filename, r.content) for r in outcome.results] == [
        ("a.md", "SHARED SECTION"),
        ("c.md", "OTHER"),
    ]
    assert outcome.duplicates_dropped == 1
    # Once a copy is skipped, the next read is a full window, not one per hit.
    assert [len(ids) for ids in store.reads] == [2, 2]


async def test_dedup_off_shows_the_shared_section_twice() -> None:
    store, tenant = await _parented(_SHARED_SECTION)
    outcome = await _search(store, tenant, limit=2, dedup=False)
    assert [r.filename for r in outcome.results] == ["a.md", "b.md"]
    assert outcome.duplicates_dropped == 0


async def test_a_child_with_no_parent_row_is_compared_by_its_own_text() -> None:
    store, tenant = await _parented(
        {
            "a.md": (["SHARED SECTION"], [(0, "SHARED", 1)]),
            "b.md": ([], [(None, "SHARED SECTION", 2)]),
            "c.md": (["OTHER"], [(0, "OTHER", 3)]),
        }
    )
    outcome = await _search(store, tenant, limit=2)
    assert [r.filename for r in outcome.results] == ["a.md", "c.md"]
    assert outcome.duplicates_dropped == 1


async def test_without_copies_parents_are_read_once_and_no_more_than_limit() -> None:
    store, tenant = await _parented(
        {
            f"{name}.md": ([f"PARENT {name}"], [(0, f"child {name}", rank)])
            for rank, name in enumerate("abcd", start=1)
        }
    )
    outcome = await _search(store, tenant, limit=2)
    assert [r.content for r in outcome.results] == ["PARENT a", "PARENT b"]
    assert [len(ids) for ids in store.reads] == [2]


def _pooled(
    store: InMemoryKnowledgeStore, *, pool: int, reranker: object = None
) -> KnowledgeRetriever:
    return KnowledgeRetriever(
        store=store,
        embedder=_Embedder(),
        reranker=reranker,  # type: ignore[arg-type]
        rerank_pool=pool,
    )


async def _vector_search(
    retriever: KnowledgeRetriever, tenant: UUID, *, limit: int
) -> KnowledgeSearchOutcome:
    return await retriever.search_detailed(
        tenant_id=tenant,
        base_names=["kb"],
        query="deductible",
        limit=limit,
        method=RetrievalMethod.VECTOR,
    )


async def test_children_past_the_rerank_pool_cost_no_parent_read() -> None:
    """The copies (c, d) rank after a rerank pool (2) already filled with distinct
    children: their parents are not read before the rerank — only the one row the
    single result needs is."""
    store, tenant = await _parented(
        {
            "a.md": (["PA"], [(0, "X", 1)]),
            "b.md": (["PB"], [(0, "Y", 2)]),
            "c.md": (["PA"], [(0, "X", 3)]),
            "d.md": (["PB"], [(0, "Y", 4)]),
        }
    )
    outcome = await _vector_search(_pooled(store, pool=2), tenant, limit=1)
    assert [r.content for r in outcome.results] == ["PA"]
    assert [len(ids) for ids in store.reads] == [1]


async def test_the_rerank_pool_is_filled_past_dropped_copies() -> None:
    """Two copies of a's passage rank right after it: the child pass looks further,
    a window at a time, until the pool (2) holds two distinct passages."""
    store, tenant = await _parented(
        {
            "a.md": (["PA"], [(0, "X", 1)]),
            "c.md": (["PA"], [(0, "X", 2)]),
            "e.md": (["PA"], [(0, "X", 3)]),
            "b.md": (["PB"], [(0, "Y", 4)]),
        }
    )
    reranker = _RecordingReranker()
    await _vector_search(_pooled(store, pool=2, reranker=reranker), tenant, limit=5)
    assert sorted(doc.rsplit("\n", 1)[-1] for doc in reranker.calls[0]) == ["X", "Y"]


async def test_a_copy_with_another_child_past_the_pool_is_counted_once() -> None:
    """c's X child is dropped before the rerank; its Z child ranks past the
    pool, so c's passage is left out again after it — one passage, counted once."""
    store, tenant = await _parented(
        {
            "a.md": (["PA"], [(0, "X", 1)]),
            "c.md": (["PA"], [(0, "X", 2), (0, "Z", 9)]),
            "b.md": (["PB"], [(0, "Y", 3)]),
            "d.md": (["PD"], [(0, "W", 4)]),
        }
    )
    outcome = await _vector_search(_pooled(store, pool=2), tenant, limit=5)
    assert [r.filename for r in outcome.results] == ["a.md", "b.md", "d.md"]
    assert outcome.duplicates_dropped == 1


async def test_many_copies_are_skipped_a_full_window_per_read() -> None:
    """Four differently cut copies of one section between the best hit and the
    next distinct one: reads of ``limit`` rows each, not one round trip per hit."""
    copies: dict[str, _Doc] = {
        f"copy{i}.md": (["SHARED"], [(0, f"SHARED part {i}", 1 + i)]) for i in range(1, 5)
    }
    store, tenant = await _parented(
        {"a.md": (["SHARED"], [(0, "SHARED", 1)]), **copies, "z.md": (["OTHER"], [(0, "OTHER", 9)])}
    )
    outcome = await _search(store, tenant, limit=2)
    assert [(r.filename, r.content) for r in outcome.results] == [
        ("a.md", "SHARED"),
        ("z.md", "OTHER"),
    ]
    assert outcome.duplicates_dropped == 4
    assert [len(ids) for ids in store.reads] == [2, 2, 2]


async def test_duplicates_dropped_counts_passages_not_chunks() -> None:
    """b.md is a.md uploaded again: its two hit children are one passage left out."""
    store, tenant = await _parented(
        {
            "a.md": (["P1 P2"], [(0, "P1", 1), (0, "P2", 2)]),
            "b.md": (["P1 P2"], [(0, "P1", 3), (0, "P2", 4)]),
        }
    )
    outcome = await _search(store, tenant)
    assert [r.filename for r in outcome.results] == ["a.md"]
    assert outcome.duplicates_dropped == 1


# ---------------------------------------------------------------------------
# What a copy must never hide
# ---------------------------------------------------------------------------

#: Two versions of a policy, one passage each: clause 3 word for word the same,
#: clause 4 changed.
_VERSIONS: dict[str, _Doc] = {
    "policy-2023.md": (
        ["Clause 3: claims within 30 days. Clause 4: cap 500."],
        [(0, "Clause 3: claims within 30 days.", 1), (0, "Clause 4: cap 500.", 8)],
    ),
    "policy-2025.md": (
        ["Clause 3: claims within 30 days. Clause 4: cap 800."],
        [(0, "Clause 3: claims within 30 days.", 2), (0, "Clause 4: cap 800.", 9)],
    ),
}


async def test_two_versions_sharing_a_clause_both_appear() -> None:
    """Recall finds only the two identical clause-3 children; the passages they
    show differ, so neither is a copy (design §7: conflicting versions are shown,
    not resolved). The parent rows read to tell are the ones the results use."""
    store, tenant = await _parented(_VERSIONS)
    outcome = await _search(store, tenant, recall_limit=2)
    assert [r.filename for r in outcome.results] == ["policy-2023.md", "policy-2025.md"]
    assert outcome.duplicates_dropped == 0
    assert [len(ids) for ids in store.reads] == [2]


class _FlatScorer:
    """Scores every candidate 0.5 and keeps their order."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return list(range(min(top_k, len(documents))))

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        return [(i, 0.5) for i in range(min(top_k, len(documents)))]


async def test_a_copy_under_a_stricter_floor_does_not_hide_the_other_bases_copy() -> None:
    """The same file in a base with a rerank floor (0.6) and one without: the
    copy that fusion ranks first fails its floor, the other must still answer."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    for name, floor in (("strict", 0.6), ("lenient", None)):
        base = await store.create_base(tenant_id=tenant, name=name, rerank_score_threshold=floor)
        document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="a.md")
        chunk = KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=0,
            content=_BODY,
            embedding=(1.0, 0.0),
        )
        await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[chunk])
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder(), reranker=_FlatScorer())
    outcome = await retriever.search_detailed(
        tenant_id=tenant,
        base_names=["strict", "lenient"],
        query="deductible",
        limit=5,
        method=RetrievalMethod.VECTOR,
    )
    assert outcome.status == "ok"
    assert [r.content for r in outcome.results] == [_BODY]
