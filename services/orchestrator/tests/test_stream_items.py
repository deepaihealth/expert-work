"""``stream_format="items"`` 的消费端转换器(对话条目 program PR3)。

覆盖 spec §五 的五条硬约束,每条一节:

* (a) item id 确定性派生,不能用自增计数器
* (b) ``item.delta`` 不带 seq
* (c) ``channel="final"`` 实时判不出来 → 先 commentary,结束时补发
* (d) 陈旧 token 的幂等抑制
* (e) ``token.step`` 在瞬时重试后会重复

**否定断言的纪律**:转换器整体失灵时「没有 ``item.added`` 了」这类断言同样
通过。所以每条否定断言前先证兄弟事件存在 —— 下面每个 ``assert not ...`` 上面
都有一条对同一批输出的肯定断言。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.runtime.runs import RunManager, RunRecord
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.sse import format_sse, sse_consumer
from orchestrator.stream_items import (
    ITEM_ADDED,
    ITEM_DELTA,
    ITEM_DONE,
    STREAM_FORMAT_ITEMS,
    ItemStreamConverter,
)

RUN = UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ai(content: str, *, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """一条已经 ``_to_jsonable`` 过的 AIMessage(实时与回放两条路径同形)。"""
    return {
        "type": "ai",
        "content": content,
        "tool_calls": calls or [],
        "additional_kwargs": {},
    }


def _tool(call_id: str, text: str, *, name: str = "search") -> dict[str, Any]:
    return {
        "type": "tool",
        "content": text,
        "tool_call_id": call_id,
        "name": name,
        "status": "success",
        "additional_kwargs": {"duration_ms": 42},
    }


def _agent_updates(step: int, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """agent 节点的 ``updates`` chunk —— ``step_count`` 是配对 token 的键。"""
    return {"agent": {"step_count": step, "messages": messages, "_duration_ms": 7}}


def _tools_updates(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """tools 节点的 ``updates`` chunk —— 没有 ``step_count``。"""
    return {"tools": {"messages": messages, "_duration_ms": 3}}


def _conv() -> ItemStreamConverter:
    return ItemStreamConverter(run_id=RUN)


def _names(frames: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in frames]


def _of_type(frames: list[tuple[str, Any]], item_type: str) -> list[dict[str, Any]]:
    return [payload for _name, payload in frames if payload.get("type") == item_type]


# ---------------------------------------------------------------------------
# 基础形态 —— 转换器到底转出了什么
# ---------------------------------------------------------------------------


def test_updates_becomes_item_done_per_message() -> None:
    """一帧 ``updates`` 扇出成每条消息一个 ``item.done``。"""
    conv = _conv()
    frames = conv.convert(
        "updates",
        _agent_updates(
            1, [_ai("先查一下", calls=[{"id": "c1", "name": "search", "args": {"q": "x"}}])]
        ),
        event_id="1700000000000-4",
    )

    assert _names(frames) == [ITEM_DONE, ITEM_DONE]
    assistant = _of_type(frames, "assistant_message")[0]
    call = _of_type(frames, "tool_call")[0]
    assert assistant["content"] == "先查一下"
    assert call["call_id"] == "c1"
    assert call["name"] == "search"
    assert call["args"] == {"q": "x"}


def test_tool_result_pairs_with_its_call_by_call_id() -> None:
    """``tool_call`` 与 ``tool_result`` 的 id 都从 ``call_id`` 派生,不靠位置。"""
    conv = _conv()
    call_frames = conv.convert(
        "updates",
        _agent_updates(1, [_ai("", calls=[{"id": "c9", "name": "search", "args": {}}])]),
        event_id="1700000000000-4",
    )
    result_frames = conv.convert(
        "updates", _tools_updates([_tool("c9", "结果")]), event_id="1700000000000-5"
    )

    call = _of_type(call_frames, "tool_call")[0]
    result = _of_type(result_frames, "tool_result")[0]
    assert call["id"] == f"{RUN}:call:c9"
    assert result["id"] == f"{RUN}:result:c9"
    assert result["call_id"] == call["call_id"] == "c9"
    assert result["duration_ms"] == 42


def test_plan_approval_error_become_single_item_done() -> None:
    """三种辅助帧各转成一个 ``item.done``,并从帧 id 的毫秒段取到时刻。"""
    conv = _conv()
    plan = conv.convert(
        "plan", {"goal": "查资料", "steps": [{"title": "一"}]}, event_id="1700000000000-2"
    )
    approval = conv.convert(
        "approval",
        {
            "request_id": "req-7",
            "node": "tools",
            "reason_kind": "policy_gate",
            "action_summary": "调用 X",
        },
        event_id="1700000000000-3",
    )
    error = conv.convert(
        "error", {"message": "boom", "name": "MaxStepsExceededError"}, event_id="1700000000000-9"
    )

    assert _names(plan + approval + error) == [ITEM_DONE, ITEM_DONE, ITEM_DONE]
    assert plan[0][1]["id"] == f"{RUN}:plan"
    assert plan[0][1]["goal"] == "查资料"
    assert approval[0][1]["id"] == f"{RUN}:approval:req-7"
    assert approval[0][1]["reason_kind"] == "policy_gate"
    assert error[0][1]["name"] == "MaxStepsExceededError"
    # 三种帧的 data 里都不含时刻,只能从 SSE id: 的毫秒段取(spec §八)。
    assert plan[0][1]["created_at"] is not None
    assert plan[0][1]["created_at"].startswith("2023-")


def test_repeated_plan_frames_upsert_one_item() -> None:
    """计划是整份快照 —— 一轮只该有一条计划条目,后来的帧覆盖前一份。"""
    conv = _conv()
    first = conv.convert("plan", {"goal": "A", "steps": []}, event_id="1700000000000-2")
    second = conv.convert("plan", {"goal": "B", "steps": []}, event_id="1700000000000-6")

    assert first[0][1]["goal"] == "A"
    assert second[0][1]["goal"] == "B"
    assert first[0][1]["id"] == second[0][1]["id"]


def test_passthrough_events_are_untouched() -> None:
    """流控与过程提示帧原样透传 —— 包括拍板留在事件形态的 ``worker``。"""
    conv = _conv()
    for name in ("metadata", "worker", "guard", "compaction", "retry"):
        payload = {"marker": name}
        assert conv.convert(name, payload, event_id="1700000000000-1") == [(name, payload)]


# ---------------------------------------------------------------------------
# (a) item id 确定性派生
# ---------------------------------------------------------------------------


def test_ids_survive_out_of_order_and_restarted_connections() -> None:
    """乱序补洞 / 跨页重连都不能改变编号。

    自增计数器在这里必红:第二个转换器从零开始数,第一个已经数到 2。这条测试
    模拟的正是 spec §四 点名的两条路径 —— 补洞重发(空洞里的号恒小于已发过
    的)与回放截断(客户端带 ``since_seq`` 新建连接)。
    """
    payload = _agent_updates(3, [_ai("答案")])

    # 连接一:先收到 seq 9,再补发乱序的 seq 4。
    first = _conv()
    first.convert("updates", _agent_updates(1, [_ai("开头")]), event_id="1700000000000-4")
    late = first.convert("updates", payload, event_id="1700000000000-9")

    # 连接二:客户端带 since_seq 重连,只收到 seq 9 这一帧。
    second = _conv()
    fresh = second.convert("updates", payload, event_id="1700000000099-9")

    assert _of_type(late, "assistant_message")[0]["id"] == f"{RUN}:step:3"
    assert _of_type(fresh, "assistant_message")[0]["id"] == f"{RUN}:step:3"


def test_positional_ids_use_seq_not_the_whole_frame_id() -> None:
    """没有 ``step_count`` 时回退到位置编号,基数只能是 seq。

    ``created_at_ms`` 在实时(bridge 的时钟)与回放(``run_event`` 行)是两次
    独立取样,毫秒级会差。把整个 ``id:`` 拿去编号,同一条目在断线重连前后就会
    换号,客户端 upsert 变成重复渲染。
    """
    chunk = {"planner": {"messages": [_ai("我想想")]}}
    live = _conv().convert("updates", chunk, event_id="1700000000123-6")
    replay = _conv().convert("updates", chunk, event_id="1700000000125-6")

    assert _of_type(live, "assistant_message")[0]["id"] == f"{RUN}:6:0"
    assert _of_type(replay, "assistant_message")[0]["id"] == f"{RUN}:6:0"


def test_token_preview_and_authoritative_item_share_one_id() -> None:
    """打字机预览与随后的权威条目必须同号,否则界面上会留两个气泡。

    同号的依据是 ``step_count``:token 帧用它、agent 节点的 ``updates`` 值也
    带它,两边都是**帧内容**,不需要任何连接级配对状态。
    """
    conv = _conv()
    preview = conv.convert("token", {"step": 2, "channel": "content", "text": "答"}, event_id=None)
    settled = conv.convert("updates", _agent_updates(2, [_ai("答案")]), event_id="1700000000000-7")

    added = next(p for n, p in preview if n == ITEM_ADDED)
    delta = next(p for n, p in preview if n == ITEM_DELTA)
    done = _of_type(settled, "assistant_message")[0]
    assert added["id"] == delta["id"] == done["id"] == f"{RUN}:step:2"


def test_tool_preview_pairs_by_call_id_not_tool_index() -> None:
    """工具卡预览用 ``call_id`` 配对。

    ``tool_index`` 在 Anthropic 路径下是**内容块**下标(前言文字块也占号),
    拿它查 ``tool_calls[]`` 数组会取到错的那个工具。这里的 ``tool_index`` 是 1
    而工具在数组里的下标是 0 —— 用错键就会配错。
    """
    conv = _conv()
    preview = conv.convert(
        "token",
        {"step": 5, "channel": "tool_args", "tool_index": 1, "call_id": "cx", "name": "search"},
        event_id=None,
    )
    settled = conv.convert(
        "updates",
        _agent_updates(
            5, [_ai("前言", calls=[{"id": "cx", "name": "search", "args": {"q": "1"}}])]
        ),
        event_id="1700000000000-8",
    )

    assert _names(preview) == [ITEM_ADDED]
    assert preview[0][1]["id"] == _of_type(settled, "tool_call")[0]["id"] == f"{RUN}:call:cx"


# ---------------------------------------------------------------------------
# (b) item.delta 不带 seq
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_delta_carries_no_sse_id() -> None:
    """``item.delta`` 上不能出现 ``id:`` 行。

    它由 ``token`` 帧转换而来,而 token 是一次性的:不记录、不占号。让一个不能
    重新发送的事件占用续传位置,客户端解析出的位点就会跑到 ``since_seq`` 实际
    能重发的范围之外,断线重连**静默漏事件**。
    """
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _record(rm)

    await bridge.publish(record.run_id, "metadata", {"run_id": str(record.run_id)})
    await bridge.publish_ephemeral(
        record.run_id, "token", {"step": 1, "channel": "content", "text": "嗨"}
    )
    await bridge.publish_end(record.run_id, status="success")

    events = await _collect(bridge, record, rm)
    # 先证兄弟事件在:带 seq 的 metadata 确实拿到了 id: 行。
    assert [name for _fid, name, _d in events if name == "metadata"] == ["metadata"]
    assert next(fid for fid, name, _d in events if name == "metadata") is not None
    deltas = [(fid, d) for fid, name, d in events if name == ITEM_DELTA]
    assert deltas, "一条 item.delta 都没有 —— 下面的断言会空转"
    assert all(fid is None for fid, _d in deltas)


@pytest.mark.asyncio
async def test_fanout_puts_the_frame_id_on_the_last_event_only() -> None:
    """一帧扇出成多帧时,``id:`` 只挂在最后一条上。

    客户端在扇出中途断线时,它记住的续传位置还停在**上一帧**,重连会把这一整
    帧重新发一遍(条目按 id upsert,重发无害)。若每一条都挂同一个 ``id:``,
    中途断线的客户端会以为这一帧已经收完,后半截条目**静默丢失**。
    """
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _record(rm)

    await bridge.publish(
        record.run_id,
        "updates",
        _agent_updates(1, [_ai("先查", calls=[{"id": "c1", "name": "search", "args": {}}])]),
    )
    await bridge.publish_end(record.run_id, status="success")

    events = await _collect(bridge, record, rm)
    fanout = [(fid, d) for fid, name, d in events if name == ITEM_DONE][:2]
    # 先证扇出真的发生了 —— 一条消息 + 一次工具调用 = 两个条目。
    assert len(fanout) == 2, f"扇出没发生,断言会空转:{events}"
    assert {d["type"] for _fid, d in fanout} == {"assistant_message", "tool_call"}
    assert fanout[0][0] is None
    assert fanout[1][0] is not None


# ---------------------------------------------------------------------------
# (c) channel="final" 实时判不出来
# ---------------------------------------------------------------------------


def test_live_assistant_messages_start_as_commentary() -> None:
    """实时一律先发 ``commentary`` —— ``final`` 要向后看一条消息,那条还不存在。"""
    conv = _conv()
    frames = conv.convert("updates", _agent_updates(1, [_ai("答案")]), event_id="1700000000000-3")

    assert _of_type(frames, "assistant_message")[0]["channel"] == "commentary"


def test_finalize_reopens_the_last_qualifying_assistant_message() -> None:
    """run 结束时补发一个 ``item.done`` 把最后一条合格的助手消息改判 ``final``。"""
    conv = _conv()
    conv.convert(
        "updates",
        _agent_updates(1, [_ai("我先查一下", calls=[{"id": "c1", "name": "s", "args": {}}])]),
        event_id="1700000000000-3",
    )
    conv.convert("updates", _tools_updates([_tool("c1", "查到了")]), event_id="1700000000000-4")
    conv.convert("updates", _agent_updates(2, [_ai("结论是 A")]), event_id="1700000000000-5")

    tail = conv.finalize()
    assert _names(tail) == [ITEM_DONE]
    assert tail[0][1]["id"] == f"{RUN}:step:2"
    assert tail[0][1]["channel"] == "final"
    assert tail[0][1]["content"] == "结论是 A"


def test_finalize_is_silent_when_the_run_ends_on_a_tool_call() -> None:
    """最后一条助手消息带工具调用时没有 ``final`` —— 它还没说完。"""
    conv = _conv()
    ok = conv.convert("updates", _agent_updates(1, [_ai("结论")]), event_id="1700000000000-3")
    assert _of_type(ok, "assistant_message"), "第一帧就没产出条目,下面的断言会空转"
    conv.convert(
        "updates",
        _agent_updates(2, [_ai("再查一下", calls=[{"id": "c2", "name": "s", "args": {}}])]),
        event_id="1700000000000-4",
    )

    assert conv.finalize() == []


def test_finalize_clears_itself() -> None:
    """重复调用不会发第二遍 —— 回放与 live 两条路径各有一个调用点。"""
    conv = _conv()
    conv.convert("updates", _agent_updates(1, [_ai("结论")]), event_id="1700000000000-3")

    assert conv.finalize(), "第一次就没补发,下面的断言会空转"
    assert conv.finalize() == []


# ---------------------------------------------------------------------------
# (d) 陈旧 token 的幂等抑制
# ---------------------------------------------------------------------------


def test_stale_token_after_the_authoritative_frame_is_dropped() -> None:
    """权威帧到过之后,同一 step 的 token 一律丢掉。

    这是「对话进行中刷新页面」那条真栈路径的核心:接合时先把已记录的事件补齐
    (于是 step 1 的条目已经 done),再挂实时流,而实时流会从缓冲区最早一条
    重新发一遍 —— 带 seq 的事件被去重挡掉,token 没有 seq,**无条件放行**。
    legacy 下这只是多看到几段陈旧的打字机文本;条目模式下它会给一个已经完成的
    条目重开 ``item.added``,界面上那段正文被打回半成品。
    """
    conv = _conv()
    settled = conv.convert(
        "updates", _agent_updates(1, [_ai("完整答案")]), event_id="1700000000000-4"
    )
    stale = conv.convert("token", {"step": 1, "channel": "content", "text": "完"}, event_id=None)

    # 先证兄弟事件在:权威帧确实产出了这个条目,所以下面不是空转。
    assert _of_type(settled, "assistant_message")[0]["id"] == f"{RUN}:step:1"
    assert stale == []


def test_stale_tool_args_token_after_the_authoritative_frame_is_dropped() -> None:
    """工具卡同理 —— 已经 done 的工具卡不能被陈旧预览重开。"""
    conv = _conv()
    settled = conv.convert(
        "updates",
        _agent_updates(1, [_ai("", calls=[{"id": "c1", "name": "search", "args": {"q": "1"}}])]),
        event_id="1700000000000-4",
    )
    stale = conv.convert(
        "token",
        {"step": 1, "channel": "tool_args", "tool_index": 0, "call_id": "c1", "name": "search"},
        event_id=None,
    )

    assert _of_type(settled, "tool_call")[0]["args"] == {"q": "1"}
    assert stale == []


def test_in_flight_step_still_streams_after_an_earlier_step_settled() -> None:
    """抑制只针对**已完成**的 step —— 正在跑的那一步照样要有打字机。

    没有这条,上一条的实现可以退化成「见过任何 updates 就不再发 token」,那才
    是真正的静默丢功能。
    """
    conv = _conv()
    conv.convert("updates", _agent_updates(1, [_ai("第一步")]), event_id="1700000000000-4")
    live = conv.convert("token", {"step": 2, "channel": "content", "text": "第二"}, event_id=None)

    assert _names(live) == [ITEM_ADDED, ITEM_DELTA]
    assert live[1][1]["text"] == "第二"


def test_suppression_survives_a_frame_that_produced_no_items() -> None:
    """整条消息被隐藏时也要记下这个 step 已完成。

    编排层写进记录的脚手架消息不产出任何条目,但那一步确实结束了 —— 抑制若挂
    在「产出过条目」上,这一支就会漏。
    """
    conv = _conv()
    hidden = _ai("脚手架")
    hidden["additional_kwargs"] = {"expert_work_hide_from_ui": True}
    settled = conv.convert("updates", _agent_updates(1, [hidden]), event_id="1700000000000-4")
    stale = conv.convert("token", {"step": 1, "channel": "content", "text": "脚"}, event_id=None)

    assert settled == []
    assert stale == []


# ---------------------------------------------------------------------------
# (e) token.step 在瞬时重试后重复
# ---------------------------------------------------------------------------


def test_retry_replays_the_same_step_without_garbling_the_preview() -> None:
    """重试从记录点重新进入,重跑的那一步 ``step`` 与上次相同。

    上一次的预览已经 done 了;若不抑制,重试的 token 会接在同一个条目后面,
    界面上得到「旧预览 + 新预览」拼起来的一段乱码。重试那次的权威事件仍会在
    新的位置到达,以同一个 id 覆盖出最终文本,所以内容不会丢。
    """
    conv = _conv()
    conv.convert("token", {"step": 2, "channel": "content", "text": "半截"}, event_id=None)
    conv.convert("updates", _agent_updates(2, [_ai("半截")]), event_id="1700000000000-5")
    conv.convert("retry", {"attempt": 1}, event_id="1700000000000-6")

    echo = conv.convert("token", {"step": 2, "channel": "content", "text": "半截"}, event_id=None)
    redo = conv.convert("updates", _agent_updates(2, [_ai("完整答案")]), event_id="1700000000000-7")

    assert echo == []
    settled = _of_type(redo, "assistant_message")[0]
    assert settled["id"] == f"{RUN}:step:2"
    assert settled["content"] == "完整答案"


# ---------------------------------------------------------------------------
# legacy 模式:一字节不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_mode_wire_is_byte_identical() -> None:
    """默认 ``stream_format`` 下,wire 与转换器引入之前逐字节一致。

    对照物是 ``format_sse`` 直接编码的字节 —— 也就是转换器分支之外那一行原本
    就在做的事。
    """
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _record(rm)

    chunk = _agent_updates(1, [_ai("你好", calls=[{"id": "c1", "name": "s", "args": {}}])])
    await bridge.publish(record.run_id, "metadata", {"run_id": str(record.run_id)})
    await bridge.publish(record.run_id, "updates", chunk)
    await bridge.publish_ephemeral(
        record.run_id, "token", {"step": 1, "channel": "content", "text": "你"}
    )
    await bridge.publish_end(record.run_id, status="success")

    raw = b""
    async for frame in sse_consumer(
        bridge=bridge,
        record=record,
        run_manager=rm,
        is_disconnected=_connected,
        heartbeat_interval=5.0,
    ):
        raw += frame

    body = raw.decode()
    assert "event: updates" in body
    assert json.dumps(chunk, separators=(",", ":")) in body
    assert format_sse("token", {"step": 1, "channel": "content", "text": "你"}) in raw
    assert ITEM_DONE not in body


@pytest.mark.asyncio
async def test_items_mode_replaces_updates_and_token() -> None:
    """条目模式下 ``updates`` / ``token`` 不再出现在 wire 上。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _record(rm)

    await bridge.publish(record.run_id, "metadata", {"run_id": str(record.run_id)})
    await bridge.publish_ephemeral(
        record.run_id, "token", {"step": 1, "channel": "content", "text": "你"}
    )
    await bridge.publish(record.run_id, "updates", _agent_updates(1, [_ai("你好")]))
    await bridge.publish_end(record.run_id, status="success")

    events = await _collect(bridge, record, rm)
    names = [name for _fid, name, _d in events]
    # 先证兄弟事件在:条目事件确实发出来了。
    assert ITEM_ADDED in names and ITEM_DELTA in names and ITEM_DONE in names
    assert "updates" not in names
    assert "token" not in names
    # 结束前补发的 final 改判。
    assert names[-2:] == [ITEM_DONE, "end"]
    assert events[-2][2]["channel"] == "final"


# ---------------------------------------------------------------------------
# 端到端小工具
# ---------------------------------------------------------------------------


async def _connected() -> bool:
    return False


async def _record(rm: RunManager) -> RunRecord:
    from expert_work.runtime.runs import DisconnectMode

    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CONTINUE,
    )


async def _collect(
    bridge: InMemoryStreamBridge, record: RunRecord, rm: RunManager
) -> list[tuple[str | None, str, Any]]:
    """跑一遍 ``sse_consumer`` 并把 wire 解析回 ``(id, event, data)``。"""
    raw = b""
    async with asyncio.timeout(10):
        async for frame in sse_consumer(
            bridge=bridge,
            record=record,
            run_manager=rm,
            is_disconnected=_connected,
            heartbeat_interval=5.0,
            stream_format=STREAM_FORMAT_ITEMS,
        ):
            raw += frame
    return _parse_sse(raw)


def _parse_sse(raw: bytes) -> list[tuple[str | None, str, Any]]:
    out: list[tuple[str | None, str, Any]] = []
    for block in raw.decode().split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        fid: str | None = None
        name = ""
        data: Any = None
        for line in block.split("\n"):
            if line.startswith("id: "):
                fid = line[4:]
            elif line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((fid, name, data))
    return out
