"""Stream CM-4 — reranker wired into long-term memory recall.

When a reranker is injected, ``memory_recall_node`` reorders the wide
candidate set (full reorder — the CM-6 MMR stage makes the final ``top_k``
cut). Rerank is best-effort — a failing reranker degrades to the RRF order
rather than dropping recall. Candidates here carry zero embeddings so the
MMR stage is order-preserving and these tests stay rerank-focused
(diversity behaviour lives in test_memory_recall_mmr).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from expert_work.protocol import MemoryItem
from orchestrator.graph_builder.memory import _MEMORY_RECALL_WIDE_LIMIT, make_memory_recall_node
from orchestrator.llm import FakeEmbedder
from orchestrator.tools.knowledge import RERANK_DOC_MAX_CHARS

_DIM = 16


def _state(task: str) -> dict[str, object]:
    return {
        "messages": [SystemMessage(content="help"), HumanMessage(content=task)],
        "step_count": 0,
        "max_steps": 5,
    }


def _mem(content: str, tenant: UUID, user: UUID) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",
        content=content,
        embedding=(0.0,) * _DIM,
    )


@dataclass
class _ListStore:
    """Returns a fixed candidate list and records the recall ``limit``."""

    items: list[MemoryItem]
    limits: list[int] = field(default_factory=list)

    async def retrieve(self, **kwargs: object) -> list[MemoryItem]:
        self.limits.append(int(kwargs["limit"]))  # type: ignore[call-overload]
        return list(self.items)

    async def bump_access(self, *, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]) -> None:
        return None


@dataclass
class _OrderReranker:
    """Returns a fixed index order; records the documents it saw."""

    order: list[int]
    seen: list[Sequence[str]] = field(default_factory=list)

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del query, tenant_id
        self.seen.append(list(documents))
        return self.order[:top_k]


@dataclass
class _BoomReranker:
    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del query, documents, top_k, tenant_id
        raise RuntimeError("reranker down")


async def _run(node: object, tenant: UUID, user: UUID) -> list[MemoryItem]:
    out = await node(  # type: ignore[operator]
        _state("what are my prefs"),
        {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
    )
    return out.get("recalled_memories", [])


@pytest.mark.asyncio
async def test_reranker_reorders_and_truncates_to_top_k() -> None:
    tenant, user = uuid4(), uuid4()
    items = [_mem(f"m{i}", tenant, user) for i in range(5)]
    store = _ListStore(items=items)
    reranker = _OrderReranker(order=[4, 2, 0])  # pick m4, m2, m0
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=3,
        reranker=reranker,  # type: ignore[arg-type]
    )

    recalled = await _run(node, tenant, user)

    assert [m.content for m in recalled] == ["m4", "m2", "m0"]
    # The reranker saw the candidate contents.
    assert reranker.seen[0] == [f"m{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_reranker_widens_recall_limit() -> None:
    tenant, user = uuid4(), uuid4()
    store = _ListStore(items=[_mem("only", tenant, user)])
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=5,
        reranker=_OrderReranker(order=[0]),  # type: ignore[arg-type]
    )
    await _run(node, tenant, user)
    # Recall always fetches max(top_k, _MEMORY_RECALL_WIDE_LIMIT) — CM-G5.
    assert store.limits == [max(5, _MEMORY_RECALL_WIDE_LIMIT)]


@pytest.mark.asyncio
async def test_no_reranker_keeps_store_order_and_wide_recall() -> None:
    tenant, user = uuid4(), uuid4()
    items = [_mem(f"m{i}", tenant, user) for i in range(3)]
    store = _ListStore(items=items)
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=5,
    )
    recalled = await _run(node, tenant, user)
    # CM-6: recall is always wide (the stores fetch that many fusion
    # candidates anyway); the store's order is preserved end-to-end.
    assert store.limits == [max(5, _MEMORY_RECALL_WIDE_LIMIT)]
    assert [m.content for m in recalled] == ["m0", "m1", "m2"]


@pytest.mark.asyncio
async def test_failing_reranker_degrades_to_rrf_order() -> None:
    tenant, user = uuid4(), uuid4()
    items = [_mem(f"m{i}", tenant, user) for i in range(4)]
    store = _ListStore(items=items)
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=2,
        reranker=_BoomReranker(),  # type: ignore[arg-type]
    )
    recalled = await _run(node, tenant, user)
    # Rerank blew up → RRF order kept, truncated to top_k. Recall not dropped.
    assert [m.content for m in recalled] == ["m0", "m1"]


@pytest.mark.asyncio
async def test_empty_recall_skips_rerank() -> None:
    tenant, user = uuid4(), uuid4()
    reranker = _OrderReranker(order=[0])
    node = make_memory_recall_node(
        memory_store=_ListStore(items=[]),  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=3,
        reranker=reranker,  # type: ignore[arg-type]
    )
    recalled = await _run(node, tenant, user)
    assert recalled == []
    assert reranker.seen == []  # rerank not called on empty recall


@pytest.mark.asyncio
async def test_reranker_gets_memories_cut_to_the_cap() -> None:
    tenant, user = uuid4(), uuid4()
    store = _ListStore(items=[_mem("m" * 5000, tenant, user), _mem("short", tenant, user)])
    reranker = _OrderReranker(order=[0, 1])
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=2,
        reranker=reranker,  # type: ignore[arg-type]
    )
    await _run(node, tenant, user)
    assert reranker.seen == [["m" * RERANK_DOC_MAX_CHARS, "short"]]
