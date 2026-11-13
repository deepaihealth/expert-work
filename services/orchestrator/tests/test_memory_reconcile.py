"""Stream CM-7 — Mem0-style reconciliation of run-end memory writes.

``flush_messages_to_memory(reconcile=True)`` checks each extracted memory
against similar existing ones and applies an explicit ADD / UPDATE /
DELETE / NOOP decision instead of writing blindly. Every failure path
degrades to a direct ADD (never lose a memory); ``reconcile=False`` is
the pre-CM-7 direct write.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder.memory import flush_messages_to_memory
from orchestrator.tools.registry import ToolSpec

_EAST = (1.0, 0.0, 0.0, 0.0)
_NEAR_EAST = (0.9, 0.43589, 0.0, 0.0)  # cosine vs _EAST = 0.9 ≥ 0.80
_NORTH = (0.0, 1.0, 0.0, 0.0)  # cosine vs _EAST = 0.0 < 0.80


@dataclass
class _MapEmbedder:
    """Embeds each text to a fixed vector from the map."""

    mapping: dict[str, tuple[float, ...]]

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self.mapping[t] for t in texts]


@dataclass
class _RecordingLLM:
    responses: list[AIMessage]
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        idx = len(self.calls)
        self.calls.append(list(messages))
        return self.responses[idx]


def _extraction(content: str) -> AIMessage:
    return AIMessage(content=f'{{"memories": [{{"kind": "fact", "content": "{content}"}}]}}')


def _ops(op: str, target: UUID | None = None) -> AIMessage:
    target_part = f', "target_id": "{target}"' if target is not None else ""
    return AIMessage(content=f'{{"ops": [{{"index": 0, "op": "{op}"{target_part}}}]}}')


async def _seed(store: InMemoryMemoryStore, tenant: UUID, user: UUID, content: str) -> MemoryItem:
    item = MemoryItem(
        id=uuid4(), tenant_id=tenant, user_id=user, kind="fact", content=content, embedding=_EAST
    )
    await store.write([item])
    return item


async def _flush(
    store: InMemoryMemoryStore,
    llm: _RecordingLLM,
    embedder: _MapEmbedder,
    tenant: UUID,
    user: UUID,
    *,
    reconcile: bool = True,
) -> int:
    return await flush_messages_to_memory(
        [HumanMessage(content="remember"), AIMessage(content="ok")],
        memory_store=store,
        embedder=embedder,  # type: ignore[arg-type]
        llm_caller=llm,
        tenant_id=tenant,
        user_id=user,
        thread_id=None,
        token=CancellationToken(),
        reconcile=reconcile,
    )


async def _live_contents(store: InMemoryMemoryStore, tenant: UUID, user: UUID) -> list[str]:
    rows = await store.retrieve(tenant_id=tenant, user_id=user, query_embedding=_EAST, limit=10)
    return sorted(m.content for m in rows)


@pytest.mark.asyncio
async def test_no_neighbor_adds_directly_without_ops_llm() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant, user, "lives in Paris")  # _EAST
    llm = _RecordingLLM(responses=[_extraction("likes jazz")])
    written = await _flush(store, llm, _MapEmbedder({"likes jazz": _NORTH}), tenant, user)
    assert written == 1
    # Only the extraction call happened — no neighbours, no ops LLM.
    assert len(llm.calls) == 1
    assert await _live_contents(store, tenant, user) == ["likes jazz", "lives in Paris"]


@pytest.mark.asyncio
async def test_update_supersedes_existing_memory() -> None:
    """P5b — a reconcile UPDATE is append-only: the old row is kept
    (invalidated, not rewritten) and a new versioned row is chained onto it
    via ``supersedes`` / ``superseded_by``."""
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    old = await _seed(store, tenant, user, "likes light roast")
    llm = _RecordingLLM(responses=[_extraction("likes dark roast"), _ops("UPDATE", old.id)])
    written = await _flush(store, llm, _MapEmbedder({"likes dark roast": _NEAR_EAST}), tenant, user)
    # B-300 D12 — applied via store.supersede (no direct write), and the row it
    # wrote counts: "written" is the rows that actually landed.
    assert written == 1
    assert len(llm.calls) == 2
    # The ops prompt carried the existing memory for the decision.
    assert str(old.id) in str(llm.calls[1][1].content)
    assert await _live_contents(store, tenant, user) == ["likes dark roast"]
    # Append-only trail: old row kept + invalidated (not overwritten), new
    # row versioned onto it.
    old_row = next(r for r in store._rows if r.id == old.id)
    assert old_row.content == "likes light roast"  # unchanged — never rewritten
    assert old_row.invalid_at is not None
    new_row = next(r for r in store._rows if r.supersedes == old.id)
    assert new_row.content == "likes dark roast"
    assert old_row.superseded_by == new_row.id


@pytest.mark.asyncio
async def test_noop_stores_nothing() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant, user, "likes coffee")
    llm = _RecordingLLM(responses=[_extraction("enjoys coffee"), _ops("NOOP")])
    written = await _flush(store, llm, _MapEmbedder({"enjoys coffee": _NEAR_EAST}), tenant, user)
    assert written == 0
    assert await _live_contents(store, tenant, user) == ["likes coffee"]


@pytest.mark.asyncio
async def test_delete_retracts_existing_without_storing_candidate() -> None:
    """P5b — a reconcile DELETE expires the target (retraction: world no
    longer true) rather than soft-deleting it (which means user-forget)."""
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    old = await _seed(store, tenant, user, "works at Acme")
    llm = _RecordingLLM(responses=[_extraction("no longer works at Acme"), _ops("DELETE", old.id)])
    written = await _flush(
        store, llm, _MapEmbedder({"no longer works at Acme": _NEAR_EAST}), tenant, user
    )
    assert written == 0
    assert await _live_contents(store, tenant, user) == []
    old_row = next(r for r in store._rows if r.id == old.id)
    assert old_row.expired_at is not None
    assert old_row.deleted_at is None  # retraction ≠ soft-delete (forget)


@pytest.mark.asyncio
async def test_malformed_ops_reply_degrades_to_add() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant, user, "likes light roast")
    llm = _RecordingLLM(
        responses=[_extraction("likes dark roast"), AIMessage(content="not json at all")]
    )
    written = await _flush(store, llm, _MapEmbedder({"likes dark roast": _NEAR_EAST}), tenant, user)
    # Never lose a memory over a parse failure — both rows live.
    assert written == 1
    assert await _live_contents(store, tenant, user) == [
        "likes dark roast",
        "likes light roast",
    ]


@pytest.mark.asyncio
async def test_unknown_update_target_degrades_to_add() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant, user, "likes light roast")
    llm = _RecordingLLM(responses=[_extraction("likes dark roast"), _ops("UPDATE", uuid4())])
    written = await _flush(store, llm, _MapEmbedder({"likes dark roast": _NEAR_EAST}), tenant, user)
    assert written == 1
    assert await _live_contents(store, tenant, user) == [
        "likes dark roast",
        "likes light roast",
    ]


@pytest.mark.asyncio
async def test_reconcile_off_writes_directly_with_single_llm_call() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant, user, "likes light roast")
    llm = _RecordingLLM(responses=[_extraction("likes dark roast")])
    written = await _flush(
        store,
        llm,
        _MapEmbedder({"likes dark roast": _NEAR_EAST}),
        tenant,
        user,
        reconcile=False,
    )
    # Pre-CM-7 behaviour: blind write, paraphrase piles up.
    assert written == 1
    assert len(llm.calls) == 1
    assert await _live_contents(store, tenant, user) == [
        "likes dark roast",
        "likes light roast",
    ]


@pytest.mark.asyncio
async def test_reconcile_update_supersedes_not_overwrites() -> None:
    """CM-7 + P5b — a reconcile UPDATE builds an append-only version chain via
    store.supersede (old row kept + invalidated), not a destructive rewrite."""
    from uuid import uuid4

    from expert_work.persistence.memory.memory import InMemoryMemoryStore
    from expert_work.protocol import MemoryItem
    from expert_work.runtime.cancellation import CancellationToken
    from orchestrator.graph_builder.memory import _apply_update

    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    old_id = uuid4()
    await store.write(
        [
            MemoryItem(
                id=old_id,
                tenant_id=tenant,
                user_id=user,
                kind="fact",
                content="user lives in Beijing",
                embedding=(1.0, 0.0, 0.0),
            )
        ]
    )
    new_item = MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",
        content="user lives in Shanghai",
        embedding=(0.9, 0.1, 0.0),
    )
    ok = await _apply_update(new_item, old_id, memory_store=store, token=CancellationToken())
    assert ok is True
    old = next(r for r in store._rows if r.id == old_id)
    assert old.invalid_at is not None and old.superseded_by == new_item.id
    new = next(r for r in store._rows if r.id == new_item.id)
    assert new.supersedes == old_id


@pytest.mark.asyncio
async def test_reconcile_delete_expires_not_softdeletes() -> None:
    from uuid import uuid4

    from expert_work.persistence.memory.memory import InMemoryMemoryStore
    from expert_work.protocol import MemoryItem
    from expert_work.runtime.cancellation import CancellationToken
    from orchestrator.graph_builder.memory import _apply_delete

    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    target = uuid4()
    await store.write(
        [
            MemoryItem(
                id=target,
                tenant_id=tenant,
                user_id=user,
                kind="fact",
                content="user owns a car",
                embedding=(1.0, 0.0, 0.0),
            )
        ]
    )
    # The candidate item is the retraction event (its content negates the fact).
    candidate = MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",
        content="user sold the car",
        embedding=(0.9, 0.1, 0.0),
    )
    ok = await _apply_delete(candidate, target, memory_store=store, token=CancellationToken())
    assert ok is True
    row = next(r for r in store._rows if r.id == target)
    assert row.expired_at is not None
    assert row.deleted_at is None  # retraction ≠ soft-delete (forget)
