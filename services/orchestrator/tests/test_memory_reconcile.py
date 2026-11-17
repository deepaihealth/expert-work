"""Stream CM-7 — Mem0-style reconciliation of run-end memory writes.

``flush_messages_to_memory(reconcile=True)`` checks each extracted memory
against similar existing ones and applies an explicit decision instead of
writing blindly — since B-300 redesign rev 9: store it or not (ADD / NOOP),
and separately which older memories it retires. A reconcile failure raises to
the job's retry (redesign PR S); ``reconcile=False`` is the pre-CM-7 direct
write. Existing memories reach the model under short
ids (``e0``, ``e1``, …, redesign §7.1 row 3).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.message_stamp import STAMP_CREATED_AT
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder.memory import flush_messages_to_memory
from orchestrator.tools.registry import ToolSpec

_EAST = (1.0, 0.0, 0.0, 0.0)
_NEAR_EAST = (0.9, 0.43589, 0.0, 0.0)  # cosine vs _EAST = 0.9 ≥ 0.80
_NORTH = (0.0, 1.0, 0.0, 0.0)  # cosine vs _EAST = 0.0 < 0.80
#: When the turn's reply was sent: later than the memories the tests seed ("now").
_SENT = datetime.now(UTC) + timedelta(hours=1)


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
    # ``turn`` 2 = 助手那一行:程序据此知道候选那一行的发送时间(不知道就退不掉任何旧记忆)。
    return AIMessage(
        content=f'{{"memories": [{{"kind": "fact", "content": "{content}", "turn": 2}}]}}'
    )


def _ops(op: str, target: str | UUID | None = None) -> AIMessage:
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
        [
            HumanMessage(content="remember"),
            AIMessage(content="ok", additional_kwargs={STAMP_CREATED_AT: _SENT.isoformat()}),
        ],
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
    # B-300 redesign §7.1 row 2 — no similarity gate: only an empty recall scope
    # skips the model (a distant neighbour still goes to it,
    # test_memory_reconcile_redesign).
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    llm = _RecordingLLM(responses=[_extraction("likes jazz")])
    written = await _flush(store, llm, _MapEmbedder({"likes jazz": _NORTH}), tenant, user)
    assert written == 1
    # Only the extraction call happened — no neighbours, no ops LLM.
    assert len(llm.calls) == 1
    assert await _live_contents(store, tenant, user) == ["likes jazz"]


@pytest.mark.asyncio
async def test_a_fuller_candidate_supersedes_the_existing_memory() -> None:
    """P5b — retiring is append-only: the old row is kept (invalidated, not
    rewritten) and the new row is chained onto it via ``supersedes`` /
    ``superseded_by``. B-300 redesign rev 9: the model no longer answers UPDATE; a
    reply that still does is read as "store the candidate, it restates e0"."""
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    old = await _seed(store, tenant, user, "likes light roast")
    llm = _RecordingLLM(responses=[_extraction("likes dark roast"), _ops("UPDATE", "e0")])
    written = await _flush(store, llm, _MapEmbedder({"likes dark roast": _NEAR_EAST}), tenant, user)
    # B-300 D12 — applied via store.supersede (no direct write), and the row it
    # wrote counts: "written" is the rows that actually landed.
    assert written == 1
    assert len(llm.calls) == 2
    # The ops prompt carried the existing memory for the decision, under its short id.
    [existing] = json.loads(str(llm.calls[1][1].content))["candidates"][0]["existing"]
    assert (existing["id"], existing["content"]) == ("e0", "likes light roast")
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
    llm = _RecordingLLM(responses=[_extraction("enjoys coffee"), _ops("NOOP", "e0")])
    written = await _flush(store, llm, _MapEmbedder({"enjoys coffee": _NEAR_EAST}), tenant, user)
    assert written == 0
    assert await _live_contents(store, tenant, user) == ["likes coffee"]


@pytest.mark.asyncio
async def test_a_denial_is_stored_and_the_old_memory_links_to_it() -> None:
    """B-300 redesign rev 9 (§7.3) — "X is no longer true" is itself stored and the old
    row is retired and linked to it. Before, the write-back expired the target and did
    not store the sentence; it no longer calls ``expire`` at all."""
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    old = await _seed(store, tenant, user, "works at Acme")
    llm = _RecordingLLM(responses=[_extraction("no longer works at Acme"), _ops("DELETE", "e0")])
    written = await _flush(
        store, llm, _MapEmbedder({"no longer works at Acme": _NEAR_EAST}), tenant, user
    )
    assert written == 1
    assert await _live_contents(store, tenant, user) == ["no longer works at Acme"]
    old_row = next(r for r in store._rows if r.id == old.id)
    new_row = next(r for r in store._rows if r.content == "no longer works at Acme")
    assert (old_row.expired_at, old_row.superseded_by) == (None, new_row.id)
    assert old_row.deleted_at is None  # retired ≠ soft-delete (forget)


@pytest.mark.asyncio
async def test_malformed_ops_reply_is_a_failure_not_a_direct_write() -> None:
    # B-300 redesign §7.6 (PR S): an unusable reconcile reply is a reconcile failure —
    # never a direct write that would bypass the tier guards and dedup. Without a job
    # (in-turn write-back) and without a DLQ it is logged; the job path retries it.
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant, user, "likes light roast")
    llm = _RecordingLLM(
        responses=[_extraction("likes dark roast"), AIMessage(content="not json at all")]
    )
    written = await _flush(store, llm, _MapEmbedder({"likes dark roast": _NEAR_EAST}), tenant, user)
    assert written == 0
    assert await _live_contents(store, tenant, user) == ["likes light roast"]


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
