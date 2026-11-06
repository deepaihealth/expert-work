"""B-285 —— the recorder files a sub-agent run under its conversation, and
``live_trajectory_keys`` drops trajectories of conversations that are gone."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage

from expert_work.protocol.trajectory_keys import trajectory_key
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryRecord, TrajectoryRecorder, live_trajectory_keys

_AT = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_sub_agent_run_is_filed_under_its_conversation() -> None:
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    tenant, conversation, sub = uuid4(), uuid4(), uuid4()
    record = TrajectoryRecord(
        thread_id=sub,
        tenant_id=tenant,
        outcome="success",
        messages=[HumanMessage(content="hi")],
        finished_at=_AT,
        conversation_id=conversation,
    )

    await recorder.record(record)

    key = trajectory_key(
        tenant_id=tenant, outcome="success", at=_AT, thread_id=sub, conversation_id=conversation
    )
    assert recorder.key_for(record) == key
    assert await store.list_prefix("trajectories/") == [key]
    assert json.loads(await store.get(key))["conversation_id"] == str(conversation)


def test_a_conversation_run_key_is_unchanged() -> None:
    tenant, thread = uuid4(), uuid4()
    record = TrajectoryRecord(
        thread_id=thread, tenant_id=tenant, outcome="failed", messages=[], finished_at=_AT
    )
    assert TrajectoryRecorder(object_store=InMemoryObjectStore()).key_for(record) == (
        f"trajectories/{tenant}/failed/2026/11/06/{thread}.jsonl"
    )


class _Threads:
    def __init__(self, alive: set[UUID]) -> None:
        self.alive = alive
        self.calls = 0

    async def get_many(
        self, thread_ids: Collection[UUID], *, tenant_id: UUID
    ) -> dict[UUID, object]:
        self.calls += 1
        return {t: object() for t in thread_ids if t in self.alive}


@pytest.mark.asyncio
async def test_only_keys_of_live_conversations_are_yielded() -> None:
    tenant, live, gone, sub = uuid4(), uuid4(), uuid4(), uuid4()
    keys = [
        trajectory_key(tenant_id=tenant, outcome="success", at=_AT, thread_id=live),
        trajectory_key(tenant_id=tenant, outcome="success", at=_AT, thread_id=gone),
        trajectory_key(
            tenant_id=tenant, outcome="success", at=_AT, thread_id=sub, conversation_id=live
        ),
        trajectory_key(
            tenant_id=tenant, outcome="success", at=_AT, thread_id=sub, conversation_id=gone
        ),
        "trajectories/not/a/key.jsonl",
    ]
    threads = _Threads({live})

    got = [k async for k in live_trajectory_keys(keys, tenant_id=tenant, threads=threads)]

    assert got == [keys[0], keys[2]]
    assert threads.calls == 1


@pytest.mark.asyncio
async def test_without_a_thread_store_every_key_passes() -> None:
    keys = ["a", "b"]
    assert [k async for k in live_trajectory_keys(keys, tenant_id=uuid4(), threads=None)] == keys
