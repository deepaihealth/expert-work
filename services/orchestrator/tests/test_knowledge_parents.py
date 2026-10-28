"""B-206 K-1 — a knowledge search returns parent passages.

Recall, fusion, rerank and the rerank-score floor run on child chunks; the
results are their parents, each once, at its best child's place and with its
scores, ``limit`` counting parents. The tool hands the model the parents' text
within a character budget that keeps its output under the externalization gate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import ChunkType, KnowledgeChunk, RetrievalMethod, chunk_retrieval_text
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, ToolContext
from orchestrator.tools.knowledge import RetrievedChunk, _format_results
from orchestrator.tools.knowledge_results import RESULTS_TEXT_BUDGET, TRUNCATED_MARK
from orchestrator.tools.overflow import EXTERNALIZE_MIN_CHARS

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Embedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0, 0.0) for _ in texts]


class _BodyScorer:
    """A scoring reranker: a document scores what ``scores`` gives the body it
    ends with (0 for none)."""

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    def _score(self, document: str) -> float:
        return next((s for body, s in self.scores.items() if document.endswith(body)), 0.0)

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
        order = sorted(range(len(documents)), key=lambda i: (-self._score(documents[i]), i))
        return [(i, self._score(documents[i])) for i in order[:top_k]]


class _InOrderReranker:
    """A reranker that ranks without scores (a chat model): keeps the order."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return list(range(len(documents)))[:top_k]


class _RaisingReranker:
    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        raise RuntimeError("rerank vendor down")

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        raise RuntimeError("rerank vendor down")


class _EmptyReranker:
    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return []

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        return []


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


#: A child's parent, by position in the parent list; ``None`` = no parent row.
#: A ``UUID`` = a parent id with no row behind it.
_ParentRef = int | UUID | None


async def _parented(
    parent_bodies: Sequence[str],
    children: Sequence[tuple[_ParentRef, str]],
    *,
    store: InMemoryKnowledgeStore | None = None,
    filename: str = "manual.md",
) -> tuple[InMemoryKnowledgeStore, UUID, list[KnowledgeChunk], list[KnowledgeChunk]]:
    """One base ``kb``, one document: parent rows (no vector) and children, child
    ``i`` with chunk index ``i`` and vector ``(1, i / 10)`` — so vector recall
    ranks the children in index order."""
    store = store if store is not None else InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=filename)
    parents = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=i,
            content=body,
            embedding=None,
            chunk_type=ChunkType.PARENT,
            heading_path=(f"Part {i}",),
        )
        for i, body in enumerate(parent_bodies)
    ]

    def parent_id(ref: _ParentRef) -> UUID | None:
        if isinstance(ref, int):
            return parents[ref].id
        return ref

    kids = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=i,
            content=body,
            embedding=(1.0, i / 10),
            parent_chunk_id=parent_id(ref),
            heading_path=(f"Part {ref}", f"child {i}") if isinstance(ref, int) else ("own",),
        )
        for i, (ref, body) in enumerate(children)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[*parents, *kids])
    return store, tenant, parents, kids


async def _search(
    store: InMemoryKnowledgeStore, tenant: UUID, *, limit: int, reranker: Any = None, **kw: Any
) -> list[RetrievedChunk]:
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder(), reranker=reranker)
    return await retriever.search(
        tenant_id=tenant,
        base_names=["kb"],
        query="q",
        limit=limit,
        method=RetrievalMethod.VECTOR,
        **kw,
    )


# ---------------------------------------------------------------------------
# Collapse to parents
# ---------------------------------------------------------------------------


async def test_children_of_one_parent_are_one_result_placed_and_scored_by_the_better() -> None:
    store, tenant, parents, kids = await _parented(
        ["PARENT A BODY", "PARENT B BODY"], [(0, "a low"), (0, "a high"), (1, "b")]
    )
    scorer = _BodyScorer({"a high": 0.9, "b": 0.8, "a low": 0.5})

    first, second = await _search(store, tenant, limit=5, reranker=scorer)

    assert (first.content, first.heading_path) == ("PARENT A BODY", ("Part 0",))
    assert (first.chunk_index, first.chunk_id) == (1, kids[1].id)
    assert (first.parent_chunk_id, first.parent_index) == (parents[0].id, 0)
    assert first.hit_chunk_indexes == (1, 0)
    assert (first.hit_content, first.hit_heading_path) == ("a high", ("Part 0", "child 1"))
    # Every score is the better child's: child 1's vector is (1, 0.1), child 0's (1, 0).
    assert first.rerank_score == 0.9
    assert first.vector_score == pytest.approx(1 / math.sqrt(1.01))
    assert first.score == first.vector_score
    assert (second.content, second.chunk_index, second.parent_index) == ("PARENT B BODY", 2, 1)
    assert (second.hit_chunk_indexes, second.rerank_score) == ((2,), 0.8)


_LAYOUT = (
    ["PARENT A", "PARENT B", "PARENT C"],
    [(0, "a1"), (0, "a2"), (0, "a3"), (1, "b1"), (2, "c1")],
)
_IN_INDEX_ORDER = {"a1": 0.9, "a2": 0.8, "a3": 0.7, "b1": 0.6, "c1": 0.5}


@pytest.mark.parametrize(
    ("reranker", "kwargs"),
    [
        pytest.param(None, {}, id="no-reranker"),
        pytest.param(_BodyScorer(_IN_INDEX_ORDER), {"rerank": False}, id="rerank-off"),
        pytest.param(_RaisingReranker(), {}, id="rerank-failed"),
        pytest.param(_EmptyReranker(), {}, id="rerank-empty"),
        pytest.param(_InOrderReranker(), {}, id="ranking-only-reranker"),
        pytest.param(_BodyScorer(_IN_INDEX_ORDER), {}, id="scoring-reranker"),
    ],
)
async def test_limit_counts_parents_on_every_rerank_path(
    reranker: Any, kwargs: dict[str, Any]
) -> None:
    """Three hits in parent A, then one in B, one in C: ``limit=2`` is A and B —
    a cut to 2 children before the collapse would leave A alone."""
    store, tenant, _parents, _kids = await _parented(*_LAYOUT)

    results = await _search(store, tenant, limit=2, reranker=reranker, **kwargs)

    assert [(r.parent_index, r.hit_chunk_indexes) for r in results] == [(0, (0, 1, 2)), (1, (3,))]
    assert [r.content for r in results] == ["PARENT A", "PARENT B"]


async def test_the_listed_parents_are_read_in_one_call() -> None:
    store, tenant, parents, _kids = await _parented(*_LAYOUT, store=_ReadRecordingStore())
    assert isinstance(store, _ReadRecordingStore)

    await _search(store, tenant, limit=2)

    assert store.reads == [[parents[0].id, parents[1].id]]


async def test_a_search_with_no_parent_rows_reads_none() -> None:
    store, tenant, _parents, _kids = await _parented(
        [], [(None, "x"), (None, "y")], store=_ReadRecordingStore()
    )
    assert isinstance(store, _ReadRecordingStore)

    await _search(store, tenant, limit=5)

    assert store.reads == []


async def test_a_child_without_a_parent_row_is_its_own_result() -> None:
    store, tenant, parents, kids = await _parented(["PARENT A"], [(None, "solo"), (0, "a1")])

    solo, parented = await _search(store, tenant, limit=5)

    assert (solo.content, solo.heading_path, solo.chunk_index) == ("solo", ("own",), 0)
    assert (solo.parent_chunk_id, solo.parent_index, solo.hit_chunk_indexes) == (None, None, (0,))
    assert (solo.hit_content, solo.hit_heading_path) == (None, ())
    assert solo.retrieval_text == chunk_retrieval_text(("own",), "solo")
    assert (parented.content, parented.parent_chunk_id) == ("PARENT A", parents[0].id)
    assert parented.chunk_id == kids[1].id


async def test_a_parent_that_cannot_be_read_falls_back_to_the_child() -> None:
    gone = uuid4()
    store, tenant, parents, _kids = await _parented(["PARENT A"], [(gone, "orphan"), (0, "a1")])

    orphan, parented = await _search(store, tenant, limit=5)

    assert (orphan.content, orphan.heading_path) == ("orphan", ("own",))
    assert (orphan.parent_chunk_id, orphan.parent_index, orphan.hit_chunk_indexes) == (
        None,
        None,
        (0,),
    )
    assert orphan.hit_content is None
    assert (parented.content, parented.parent_chunk_id) == ("PARENT A", parents[0].id)


async def test_the_floor_applies_to_children_before_they_are_collapsed() -> None:
    """A child under the base's rerank-score floor is no hit: its parent comes
    back on its other child alone, and a parent with no child over the floor
    does not come back."""
    store, tenant, _parents, _kids = await _parented(
        ["PARENT A", "PARENT B", "PARENT C"], [(0, "a1"), (0, "a2"), (1, "b1"), (2, "c1")]
    )
    base = await store.get_base(tenant_id=tenant, name="kb")
    assert base is not None
    await store.update_base(tenant_id=tenant, kb_id=base.id, rerank_score_threshold=0.5)
    scorer = _BodyScorer({"a1": 0.4, "a2": 0.6, "b1": 0.55, "c1": 0.3})

    results = await _search(store, tenant, limit=5, reranker=scorer)

    assert [(r.content, r.chunk_index, r.hit_chunk_indexes) for r in results] == [
        ("PARENT A", 1, (1,)),
        ("PARENT B", 2, (2,)),
    ]


async def test_the_tool_shows_each_parent_once_under_its_best_childs_label() -> None:
    # Child 0 is found by both paths, child 1 by its vector alone: child 0 ranks first.
    store, tenant, _parents, _kids = await _parented(
        ["PARENT A BODY"], [(0, "deductible one"), (0, "other two")]
    )
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder())
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))

    result = await tool.call({"query": "deductible"}, ctx=ToolContext(tenant_id=tenant))

    assert result.content == "[D1#0] manual.md\n[Section: Part 0]\n\nPARENT A BODY"
    assert result.meta["hits"] == 1


# ---------------------------------------------------------------------------
# The tool's text budget
# ---------------------------------------------------------------------------


def _result(
    filename: str, index: int, text: str, *, hit: str | None = None, parent: int | None = None
) -> RetrievedChunk:
    """A result whose parent body is ``text`` and best child body ``hit`` (no
    parent row when ``hit`` is ``None``)."""
    return RetrievedChunk(
        content=text,
        filename=filename,
        chunk_index=index,
        parent_chunk_id=None if hit is None else uuid4(),
        parent_index=parent if hit is not None else None,
        hit_chunk_indexes=(index,),
        hit_content=hit,
    )


def test_results_without_parents_render_as_before() -> None:
    results = [
        RetrievedChunk(content="body zero", filename="a.md", chunk_index=0, heading_path=("H",)),
        RetrievedChunk(content="body one", filename="b.md", chunk_index=3),
    ]

    assert _format_results(results) == (
        "[a.md#0]\n[Section: H]\n\nbody zero\n\n---\n\n[b.md#3]\nbody one"
    )


def test_a_parent_over_what_is_left_shows_its_hit_child_marked_truncated() -> None:
    first = _result("a.md", 0, "A" * 6000, hit="a", parent=0)
    second = _result("b.md", 4, "B" * 6000, hit="the hit child", parent=1)
    third = _result("c.md", 7, "small parent", hit="c", parent=2)

    text = _format_results([first, second, third])

    assert text.split("\n\n---\n\n") == [
        "[a.md#0]\n" + "A" * 6000,
        "[b.md#4]\nthe hit child\n[已截断]",
        "[c.md#7]\nsmall parent",
    ]
    assert len(text) <= RESULTS_TEXT_BUDGET


def test_headers_and_separators_count_against_the_budget() -> None:
    """A first block of 5,000 characters leaves 5,000 - 7 (the separator) for the
    next block, its ``[b.md#1]`` header line included: 4,984 characters of text
    fit exactly, 4,985 do not."""
    first = _result("a.md", 0, "A" * (5000 - len("[a.md#0]\n")), hit="a", parent=0)
    exact = _result("b.md", 1, "B" * 4984, hit="b", parent=1)
    over = _result("b.md", 1, "B" * 4985, hit="b", parent=1)

    assert len(_format_results([first, exact])) == RESULTS_TEXT_BUDGET
    assert _format_results([first, over]).endswith("[b.md#1]\nb\n[已截断]")


@pytest.mark.parametrize(
    "stopper",
    [
        pytest.param(_result("b.md", 1, "B" * 2000, hit="b" * 1000, parent=1), id="parent"),
        pytest.param(_result("b.md", 1, "b" * 1000), id="no-parent"),
    ],
)
def test_listing_stops_at_the_first_result_of_which_nothing_fits(
    stopper: RetrievedChunk,
) -> None:
    """Lower-ranked results are dropped, not skipped over: the small one after
    the result that does not fit is not listed either."""
    first = _result("a.md", 0, "A" * 9500, hit="a", parent=0)
    tiny = _result("c.md", 2, "tiny", hit="c", parent=2)

    assert _format_results([first, stopper, tiny]) == "[a.md#0]\n" + "A" * 9500


@pytest.mark.parametrize(
    ("result", "shown"),
    [
        pytest.param(_result("a.md", 3, "P" * 30_000, hit="h" * 15_000, parent=0), "h", id="hit"),
        pytest.param(_result("a.md", 3, "o" * 15_000), "o", id="no-parent"),
    ],
)
def test_the_first_result_always_appears_cut_to_the_budget(
    result: RetrievedChunk, shown: str
) -> None:
    text = _format_results([result, _result("b.md", 0, "tiny", hit="t", parent=1)])

    assert text.startswith(f"[a.md#3]\n{shown * 100}")
    assert text.endswith(f"{shown}\n{TRUNCATED_MARK}")
    assert len(text) == RESULTS_TEXT_BUDGET


async def test_twenty_full_size_parents_stay_under_the_externalization_gate() -> None:
    """``limit=20`` over parents at their 1,024-token cap — about 4,000+
    characters of English each — with one 400-token hit child each."""
    bodies = [f"{i:02d} " + "parent text " * 420 for i in range(20)]
    store, tenant, _parents, _kids = await _parented(
        bodies, [(i, f"{i:02d} " + "child text " * 150) for i in range(20)]
    )
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder())
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))

    result = await tool.call({"query": "q", "limit": 20}, ctx=ToolContext(tenant_id=tenant))

    assert result.meta["hits"] == 20
    assert len(result.content) < EXTERNALIZE_MIN_CHARS
    assert len(result.content) <= RESULTS_TEXT_BUDGET
    assert result.content.startswith(f"[D1#0] manual.md\n[Section: Part 0]\n\n{bodies[0]}")
    # The second parent does not fit what is left: its hit child, breadcrumb and all.
    assert "\n\n---\n\n[D1#1] manual.md\n[Section: Part 1 > child 1]\n\n01 child text" in (
        result.content
    )
    assert TRUNCATED_MARK in result.content
