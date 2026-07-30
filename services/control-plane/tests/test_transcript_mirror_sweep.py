"""Unit tests for the transcript mirror sweep + shared extraction (IA M4).

Drives ``TranscriptMirrorSweep.run_once`` over a stubbed checkpointer and
an in-memory message store whose ``pending_thread_ids`` is overridden (the
real work-queue selection is SQL-only and covered by the persistence
integration tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.transcript import read_turns
from control_plane.transcript_mirror_sweep import TranscriptMirrorSweep
from expert_work.persistence import InMemoryThreadMessageStore

THREAD_ID = uuid4()


def _msg(mtype: str, content: Any) -> SimpleNamespace:
    return SimpleNamespace(type=mtype, content=content)


class _FakeCheckpointer:
    """aget_tuple stub — thread_id → message list (None = no checkpoint)."""

    def __init__(self, by_thread: dict[str, list[SimpleNamespace] | None]) -> None:
        self._by_thread = by_thread

    async def aget_tuple(self, config: dict[str, Any]) -> SimpleNamespace | None:
        thread_id = config["configurable"]["thread_id"]
        messages = self._by_thread.get(thread_id)
        if messages is None:
            return None
        return SimpleNamespace(checkpoint={"channel_values": {"messages": messages}})


def _tc(name: str) -> dict[str, Any]:
    return {"name": name, "args": {}, "id": "call_1"}


def _checkpointer_with(msgs: list[HumanMessage | AIMessage]) -> _FakeCheckpointer:
    return _FakeCheckpointer({str(THREAD_ID): msgs})


class _QueueStore(InMemoryThreadMessageStore):
    """In-memory store with an injectable work queue."""

    def __init__(self, queue: list[tuple[UUID, UUID]]) -> None:
        super().__init__()
        self.queue = queue
        self.synced: list[tuple[UUID, datetime]] = []

    async def pending_thread_ids(self, *, limit: int) -> list[tuple[UUID, UUID]]:
        return self.queue[:limit]

    async def sync_thread(self, *, thread_id, tenant_id, turns, synced_at) -> None:  # type: ignore[no-untyped-def]
        self.synced.append((thread_id, synced_at))
        await super().sync_thread(
            thread_id=thread_id, tenant_id=tenant_id, turns=turns, synced_at=synced_at
        )


def _sweep(store: InMemoryThreadMessageStore, checkpointer: object | None) -> TranscriptMirrorSweep:
    runtime = SimpleNamespace(durable_checkpointer=checkpointer)
    return TranscriptMirrorSweep(message_store=store, runtime=runtime)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_read_turns_filters_to_text_turns_with_stable_seq() -> None:
    cp = _FakeCheckpointer(
        {
            "t1": [
                _msg("human", "I was charged twice"),
                _msg("ai", [{"type": "text", "text": "Refund opened"}]),
                _msg("tool", "lookup result"),  # skipped
                _msg("ai", "   "),  # empty text — skipped
                _msg("human", "thanks"),
            ]
        }
    )
    turns = await read_turns(cp, UUID(int=0))  # type: ignore[arg-type]
    assert turns == []  # unknown thread → no checkpoint → empty

    class _Fixed(_FakeCheckpointer):
        async def aget_tuple(self, config: dict[str, Any]) -> SimpleNamespace | None:
            return SimpleNamespace(
                checkpoint={"channel_values": {"messages": self._by_thread["t1"]}}
            )

    turns = await read_turns(_Fixed(cp._by_thread), uuid4())  # type: ignore[arg-type]
    # seq is the raw channel index — gaps where non-text turns were skipped.
    assert [(t.seq, t.role) for t in turns] == [(0, "user"), (1, "assistant"), (4, "user")]
    assert turns[1].content == "Refund opened"


@pytest.mark.asyncio
async def test_read_turns_keeps_hidden_faithful_by_default() -> None:
    """RT-2 PR-4 (RT-ADR-9), Option A — the faithful default
    (``include_hidden=True``) keeps orchestrator scaffolding tagged
    ``expert_work_hide_from_ui`` (the CM-1 ``<recovery-advisory>``) in the record,
    so the search/audit mirror and the cross-tenant audit drill-in see the
    complete transcript. This is the deer-flow pattern: the durable record
    stays faithful; visibility is a serve-boundary concern, not extraction."""
    tid = uuid4()
    hidden = SimpleNamespace(
        type="human",
        content="<recovery-advisory>internal guidance</recovery-advisory>",
        additional_kwargs={"expert_work_hide_from_ui": True},
    )
    cp = _FakeCheckpointer(
        {
            str(tid): [
                _msg("human", "real question"),
                hidden,  # index 1 — kept when faithful
                _msg("ai", "real answer"),
            ]
        }
    )
    faithful = await read_turns(cp, tid)  # type: ignore[arg-type]
    assert [(t.seq, t.role, t.content) for t in faithful] == [
        (0, "user", "real question"),
        (1, "user", "<recovery-advisory>internal guidance</recovery-advisory>"),
        (2, "assistant", "real answer"),
    ]


@pytest.mark.asyncio
async def test_read_turns_filters_hidden_only_for_ui_bubble_view() -> None:
    """RT-2 PR-4 (RT-ADR-9), Option A — only the UI bubble view opts out
    (``include_hidden=False``): scaffolding never renders as a user/assistant
    turn, while the ordinary turns keep their raw channel index as ``seq``."""
    tid = uuid4()
    hidden = SimpleNamespace(
        type="human",
        content="<recovery-advisory>internal guidance</recovery-advisory>",
        additional_kwargs={"expert_work_hide_from_ui": True},
    )
    cp = _FakeCheckpointer(
        {
            str(tid): [
                _msg("human", "real question"),
                hidden,  # index 1 — filtered from the UI view
                _msg("ai", "real answer"),
            ]
        }
    )
    ui = await read_turns(cp, tid, include_hidden=False)  # type: ignore[arg-type]
    assert [(t.seq, t.role, t.content) for t in ui] == [
        (0, "user", "real question"),
        (2, "assistant", "real answer"),
    ]


@pytest.mark.asyncio
async def test_run_once_mirrors_pending_threads() -> None:
    tenant = uuid4()
    ok_thread, broken_thread = uuid4(), uuid4()
    store = _QueueStore([(ok_thread, tenant), (broken_thread, tenant)])

    class _PartiallyBroken(_FakeCheckpointer):
        async def aget_tuple(self, config: dict[str, Any]) -> SimpleNamespace | None:
            if config["configurable"]["thread_id"] == str(broken_thread):
                msg = "checkpoint read failed"
                raise RuntimeError(msg)
            return SimpleNamespace(
                checkpoint={"channel_values": {"messages": [_msg("human", "find me")]}}
            )

    sweep = _sweep(store, _PartiallyBroken({}))
    before = datetime.now(UTC)
    synced = await sweep.run_once()

    # The broken thread is skipped (retried next cycle), the healthy one lands.
    assert synced == 1
    assert await store.search_thread_ids(tenant_id=tenant, q="find me") == {ok_thread}
    # Watermark is taken BEFORE the checkpoint read — a run landing mid-read
    # bumps agent_run.updated_at past it, so the tail is re-selected.
    (thread_synced, mark) = store.synced[0]
    assert thread_synced == ok_thread
    assert mark >= before


@pytest.mark.asyncio
async def test_run_once_short_circuits_without_checkpointer() -> None:
    store = _QueueStore([(uuid4(), uuid4())])
    sweep = _sweep(store, None)
    assert await sweep.run_once() == 0
    assert store.synced == []


@pytest.mark.asyncio
async def test_read_turns_channels_commentary_and_final() -> None:
    """段内末条且无 tool_calls = final;带 tool_calls / 非末条 = commentary。"""
    msgs = [
        HumanMessage(content="写两章综述"),
        AIMessage(content="先搜第一章资料", tool_calls=[_tc("web_search")]),
        AIMessage(content="第一章正文…现在搜第二章", tool_calls=[_tc("web_search")]),
        AIMessage(content="第二章正文,全文完。"),
        HumanMessage(content="再补个结论"),
        AIMessage(content="补充搜索", tool_calls=[_tc("web_search")]),
    ]
    turns = await read_turns(_checkpointer_with(msgs), THREAD_ID)
    assert [(t.role, t.channel) for t in turns] == [
        ("user", None),
        ("assistant", "commentary"),
        ("assistant", "commentary"),
        ("assistant", "final"),
        ("user", None),
        # 第二段末条带 tool_calls(暂停/未完轮)→ 无 final
        ("assistant", "commentary"),
    ]


@pytest.mark.asyncio
async def test_read_turns_channel_segments_reset_at_user_boundary() -> None:
    """reflect 打回形态:候选答案被追加消息取代后自动变 commentary。"""
    msgs = [
        HumanMessage(content="任务"),
        AIMessage(content="候选答案 v1"),  # 无 tool_calls,但非段末条
        HumanMessage(
            content="[Reflection] 不够好",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
        AIMessage(content="答案 v2"),
    ]
    turns = await read_turns(_checkpointer_with(msgs), THREAD_ID, include_hidden=False)
    assert [(t.role, t.channel) for t in turns] == [
        ("user", None),
        ("assistant", "commentary"),  # 隐藏行剔除后仍非末条
        ("assistant", "final"),
    ]
