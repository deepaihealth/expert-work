"""三路同源黄金测试 —— 对话条目 program PR4。

设计见 ``docs/superpowers/specs/2026-08-25-conversation-items-design.md`` §四。
条目从三条路径产出,任何一条漂了,第三方就会看到「实时和刷新之后不一样」。
spec 的原话:**没有这个测试,同源只是口头承诺**。

被驱动的三条路径
----------------

============= =============================================================
路径           入口
============= =============================================================
实时 SSE       ``orchestrator.sse.sse_consumer``,``stream_format="items"``
单 run 回放     ``control_plane.api._run_event_stream.build_event_producer``
会话历史       ``GET /v1/agents/{code}/sessions/{id}/items``
============= =============================================================

跑的是**真 run**:三张真的 LangGraph 图(见 ``items_parity_fixtures``),编译时
挂上会话共用的那个 checkpointer,由 ``run_agent`` 驱动。所以 ``updates`` 帧是
LangGraph 自己产出的、落库行是 ``run_agent`` 自己写的、检查点消息是图自己写的
—— 三条路径的输入没有一条是测试手工摆出来的。

比对的是**最终条目集合,不是事件序列**
--------------------------------------

三条路径的事件序列本来就不对称(spec §五「修正 —— 三条路径的事件序列不
对称」),这不是 bug:

* 实时:``item.added`` → ``item.delta``* → ``item.done``
* 单 run 回放:只有 ``item.done``(回放里根本没有 token 帧)
* live 接合:补库段只有 done,接上实时后才有完整三段

所以 :func:`_fold` 按客户端 reducer 的语义(``item.done`` = upsert)把生命
周期事件折成最终条目列表,再比对。``item.done`` 是权威态。

四处**有意**不比对的地方,每一处都单独立了一条断言,不许悄悄抹平
--------------------------------------------------------------

1. **``id`` 不跨路径承诺**(见 ``conversation_items`` 模块 docstring:只保证
   同一响应内唯一、同一查询可重复)。比对时剔除,但
   :func:`test_item_ids_are_unique_within_each_path` 单独钉住每条路径**内部**
   的唯一性 —— 客户端拿它当 key。

2. **``user_message`` 只有历史给得出**。它是 graph 的输入,从没进过事件流
   (spec §一 第 2 条),实时与回放物理上产不出这条。
   :func:`test_user_message_is_history_only` 正面钉住这个不对称。

3. **``tool_call.worker`` 只有历史会填** —— spec §五 拍板的「唯一不完全同构
   处」。见 :func:`test_worker_never_leaks_into_live_or_replay`。

4. **``plan`` / ``approval`` / ``error`` 三种条目的 ``created_at``** —— 见下。

``created_at`` 的处置(brief 点名的那个坑)
------------------------------------------

这三种帧的 ``data`` 里都不含时刻,时刻只在 SSE 的 ``id:`` 前缀上,而两条路径
取的是**两次独立采样**:

* 回放 / 历史 —— ``run_event`` 行的落库时刻。``make_event_record`` 用同一个
  ``created_at_ms`` 同时算出 ``created_at``,所以这**两条路径必须逐字节相等**,
  没有任何容差可言(:func:`test_aux_created_at_replay_equals_history_exactly`)。
* 实时 —— 没有落库时刻可用,PR3 取的是 bridge ``publish`` 时 ``id:`` 里的发布
  时钟。``_enqueue_event`` 紧接着又取一次 ``time.time()``,所以实时与落库是同
  一瞬间的两次采样。

**选的做法**:只对「实时 ↔ 落库」这**一对**放宽到
:data:`_AUX_CLOCK_TOLERANCE_MS` 毫秒,另一对(回放 ↔ 历史)照旧逐字节相等。
理由是这两次采样中间只隔一个 ``bridge.publish`` 返回加一次 ``put_nowait``,
实测同毫秒;把容差只发给真正存在两次采样的那一对,比整体排除 ``created_at``
少放过一个数量级的错。

**这个放宽会漏掉什么**:它对「实时侧根本没读帧的时刻、而是在转发那一刻取了
一次 ``now``」这类 bug 不敏感 —— 转发紧跟着发布,差值同样落在容差内。
(实测过:把 ``_frame_created_at`` 换成 ``datetime.now`` 之后,这条容差断言
**照样是绿的**。)所以另配一条闸把这个盲区堵上:
:func:`test_live_aux_created_at_is_the_frame_clock` 在 run 结束后先记一个时刻
再去消费实时流,断言实时给出的 ``created_at`` 一律 **≤ 那个时刻**。取「转发时
的 now」会晚于它,当场变红 —— 同一处变异下它确实红了。

空结果下恒真的防范
------------------

三条路径都产出空列表时「三者相等」同样通过。所以
:func:`test_golden_run_is_multi_step_with_tools` 先把 fixture 的形状逐条钉死
(多步、带工具调用、多条 assistant_message —— spec §四 的硬要求),核心比对
再断言相等。fixture 被谁改弱了,先红的是那条。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.api._run_event_stream import EXTERNAL_HIDDEN_EVENTS, build_event_producer
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.approval import InMemoryApprovalStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunRecord,
    RunStatus,
)
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.sse import _BACKGROUND_PERSIST_WRITERS, run_agent, sse_consumer
from orchestrator.stream_items import ITEM_ADDED, ITEM_DELTA, ITEM_DONE, STREAM_FORMAT_ITEMS
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import build_test_jwt_verifier, make_test_jwt
from tests.items_parity_fixtures import (
    AGENT_SPEC,
    DELEGATED_CALL_ID,
    PLAN_V1,
    PLAN_V2,
    T0,
    approval_graph,
    build_settings,
    failing_graph,
    multi_step_graph,
    stamp,
)
from tests.test_run_event_stream import _parse_sse

#: 实时 ↔ 落库两次时钟采样的容差(毫秒)。见模块 docstring 的「``created_at``
#: 的处置」。两次采样中间只隔一个 ``publish`` 返回加一次 ``put_nowait``,给到
#: 秒级已经是几个数量级的余量,纯粹为了在负载高的 CI 机器上不 flake。
_AUX_CLOCK_TOLERANCE_MS = 1000

#: 时刻不在 ``data`` 里、只在 SSE ``id:`` 前缀上的三种条目(spec §八)。
_AUX_TYPES = frozenset({"plan", "approval", "error"})

_USER_MESSAGE = "user_message"

#: 比对时剔除的字段。``id`` 不跨路径承诺;``worker`` 是 spec §五 拍板的
#: 「唯一不完全同构处」(只有历史填)。两者各有一条独立断言兜着。
_NOT_COMPARED = frozenset({"id", "worker"})


async def _never_disconnected() -> bool:
    return False


async def _await_persist_writers() -> None:
    """等后台落库 writer 把队列刷干净 —— 回放路径读的就是它写的行。"""
    if _BACKGROUND_PERSIST_WRITERS:
        await asyncio.gather(*_BACKGROUND_PERSIST_WRITERS, return_exceptions=True)


@dataclass
class _Turn:
    """一轮 run 在三条路径上的产出。"""

    run_id: UUID
    status: RunStatus
    live: list[tuple[str | None, str, Any]] = field(default_factory=list)
    replay: list[tuple[str | None, str, Any]] = field(default_factory=list)


@dataclass
class _Golden:
    session_id: UUID
    turns: list[_Turn]
    #: ``run_id`` → 历史接口给出的条目,保持接口返回的顺序。
    history: dict[str, list[dict[str, Any]]]
    #: run 全部结束、开始消费实时流**之前**的墙钟(秒)。
    settled_at: float


@pytest.fixture
async def golden() -> AsyncIterator[_Golden]:
    """跑完三轮真 run,再把三条路径的产出全收上来。

    三轮共用一段会话、一个检查点、一个事件库、一个 bridge —— 与生产同构。
    第二、三轮因此会看到第一轮留下的 ``plan``(``run_agent`` 的冷启动补发),
    这正是生产形态。
    """
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    event_store = InMemoryRunEventStore()
    approvals = InMemoryApprovalStore()
    app = create_app(
        settings=build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=event_store),
        run_repo=run_store,
        run_event_repo=event_store,
        approval_repo=approvals,
    )
    checkpointer = InMemorySaver()
    app.state.agent_runtime.durable_checkpointer = checkpointer
    bridge: InMemoryStreamBridge = app.state.agent_runtime.stream_bridge
    run_manager = app.state.agent_runtime.run_manager
    tenant_id = uuid4()
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await app.state.agent_spec_repo.create(
            tenant_id=tenant_id,
            spec=AgentSpec.model_validate(deepcopy(AGENT_SPEC)),
            spec_sha256="a" * 64,
            created_by="seed",
        )
        # 开一段归属正确的会话 —— ``load_owned_session`` 认的是 thread meta。
        started = await client.post(
            "/v1/agents/support-bot/runs",
            json={"user_id": "u-123", "input": "hi", "mode": "queue"},
            headers=headers,
        )
        assert started.status_code == 202, started.text
        session_id = UUID(started.json()["data"]["thread_id"])
        seed_run = UUID(started.json()["data"]["run_id"])
        seed_row = await run_store.get(run_id=seed_run, tenant_id=tenant_id)
        assert seed_row is not None
        await run_store.set_status(
            run_id=seed_run,
            tenant_id=tenant_id,
            status=RunStatus.SUCCESS,
            updated_at=seed_row.created_at,
            finished_at=seed_row.created_at,
        )

        records: list[RunRecord] = []

        async def _drive(build: Any, *, prompt: str, secs: int, with_system: bool) -> RunRecord:
            run_id = uuid4()
            record = await run_manager.create(
                run_id=run_id,
                thread_id=session_id,
                tenant_id=tenant_id,
                on_disconnect=DisconnectMode.CANCEL,
            )
            inbound: list[BaseMessage] = []
            if with_system:
                inbound.append(SystemMessage(content="you are support"))
            inbound.append(stamp(HumanMessage(content=prompt), run_id=run_id, secs=secs))
            await run_agent(
                bridge=bridge,
                run_manager=run_manager,
                record=record,
                graph=build(run_id, checkpointer),
                graph_input={"messages": inbound},
                config={"configurable": {"thread_id": str(session_id), "checkpoint_ns": ""}},
                event_store=event_store,
                approval_store=approvals,
            )
            records.append(record)
            return record

        await _drive(
            multi_step_graph,
            prompt="北京和上海今天多少度?顺便把北京的换算成华氏。",
            secs=0,
            with_system=True,
        )
        await _drive(approval_graph, prompt="帮我把报告发给客户。", secs=10, with_system=False)
        await _drive(failing_graph, prompt="再算一次。", secs=20, with_system=False)
        await _await_persist_writers()

        # run 全部结束的时刻。实时流在这之后才消费 —— 见模块 docstring 里
        # 「这个放宽会漏掉什么」那一段,这个时刻是那条闸的基准。
        settled_at = time.time()
        await asyncio.sleep(0.01)

        turns: list[_Turn] = []
        for record in records:
            turn = _Turn(run_id=record.run_id, status=record.status)
            turn.live = _parse_sse(
                [
                    chunk
                    async for chunk in sse_consumer(
                        bridge=bridge,
                        record=record,
                        run_manager=run_manager,
                        is_disconnected=_never_disconnected,
                        heartbeat_interval=30.0,
                        hide_events=EXTERNAL_HIDDEN_EVENTS,
                        stream_format=STREAM_FORMAT_ITEMS,
                    )
                ]
            )
            plan = await build_event_producer(
                run_id=record.run_id,
                run_status=record.status,
                event_store=event_store,
                stream_bridge=bridge,
                since_seq=None,
                scope=None,
                hide_events=EXTERNAL_HIDDEN_EVENTS,
                stream_format=STREAM_FORMAT_ITEMS,
            )
            turn.replay = _parse_sse([chunk async for chunk in plan.producer])
            turns.append(turn)

        resp = await client.get(
            f"/v1/agents/support-bot/sessions/{session_id}/items",
            params={"user_id": "u-123", "limit": 20},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        history: dict[str, list[dict[str, Any]]] = {}
        for item in resp.json()["data"]["items"]:
            history.setdefault(item["run_id"], []).append(item)

        yield _Golden(session_id=session_id, turns=turns, history=history, settled_at=settled_at)


# ---------------------------------------------------------------------------
# 折叠 / 归一
# ---------------------------------------------------------------------------


def _fold(frames: list[tuple[str | None, str, Any]]) -> list[dict[str, Any]]:
    """生命周期事件 → 最终条目列表,按客户端 reducer 的语义。

    ``item.added`` 建条目、``item.done`` **upsert**(允许对一个从没 added 过的
    id 直接 done —— 回放路径只有 done)。``item.delta`` 是流式预览,不进最终
    态:它带的是增量文本,权威内容随后的 ``item.done`` 会整份给出。

    顺序取**首次出现**的位置 —— 这正是客户端列表里气泡的位置,后来的 upsert
    只改内容不搬家。
    """
    order: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    for _fid, name, data in frames:
        if name not in (ITEM_ADDED, ITEM_DONE):
            continue
        item_id = data["id"]
        if item_id not in latest:
            order.append(item_id)
        latest[item_id] = data
    return [latest[i] for i in order]


def _content(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """比对用的形状:剔除 :data:`_NOT_COMPARED`,并把三种辅助条目的
    ``created_at`` 摘出去。

    每一处剔除都有一条独立断言兜着(模块 docstring「四处**有意**不比对的
    地方」),这里不是悄悄抹平。
    """
    out: list[dict[str, Any]] = []
    for item in items:
        shape = {k: v for k, v in item.items() if k not in _NOT_COMPARED}
        if item["type"] in _AUX_TYPES:
            shape.pop("created_at", None)
        out.append(shape)
    return out


def _aux_times(items: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(i["type"], i["created_at"]) for i in items if i["type"] in _AUX_TYPES]


def _types(items: list[dict[str, Any]]) -> list[str]:
    return [i["type"] for i in items]


def _without_user(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["type"] != _USER_MESSAGE]


def _ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000.0


# ---------------------------------------------------------------------------
# fixture 自身的形状 —— 先钉住它,后面的「三者相等」才不是空转
# ---------------------------------------------------------------------------


def test_golden_run_is_multi_step_with_tools(golden: _Golden) -> None:
    """黄金 run 必须是**多步、带工具调用、带多条 assistant_message** 的。

    spec §四 的硬要求。只跑一问一答时,实时侧对 ``channel`` 的局部判定(无
    tool_calls ⟹ final)恰好与历史一致,「三者相等」会全绿而 bug 仍在。谁把
    fixture 改弱了,先红的是这一条。
    """
    first = _fold(golden.turns[0].live)
    assert _types(first) == [
        "plan",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
        "tool_call",
        "tool_call",  # 一条 AIMessage 带两个调用(spec §八)
        "tool_result",
        "tool_result",
        "assistant_message",
    ], first

    assistants = [i for i in first if i["type"] == "assistant_message"]
    assert len(assistants) == 3
    # 中间说明与最终正文必须**判得不一样** —— 两者都是 commentary 的话,
    # 「三条路径一致」就退化成了「三条路径一样瞎」。
    assert [a["channel"] for a in assistants] == ["commentary", "commentary", "final"]

    # 工具调用与结果靠 ``call_id`` 配对,不靠相邻位置。
    calls = {i["call_id"] for i in first if i["type"] == "tool_call"}
    results = {i["call_id"] for i in first if i["type"] == "tool_result"}
    assert calls == results == {"call-1", "call-2", "call-3"}

    # 三轮合起来把三种辅助条目全覆盖了。
    all_types = {t for turn in golden.turns for t in _types(_fold(turn.live))}
    assert _AUX_TYPES <= all_types, all_types


def test_history_covers_every_run(golden: _Golden) -> None:
    """三轮都进了历史 —— 否则下面按 run 比对会静默跳过整轮。"""
    assert {str(t.run_id) for t in golden.turns} <= set(golden.history), golden.history
    assert [t.status for t in golden.turns] == [
        RunStatus.SUCCESS,
        RunStatus.PAUSED,
        RunStatus.ERROR,
    ]


# ---------------------------------------------------------------------------
# 核心 —— 三条路径的最终条目集合必须一致
# ---------------------------------------------------------------------------


def test_three_paths_agree_on_item_content(golden: _Golden) -> None:
    """同一次 run,实时 / 回放 / 历史三条路径产出的条目必须一致。

    这是整个设计核心主张的唯一守卫。剔除的只有 :data:`_NOT_COMPARED`
    (``id`` / ``worker``)、历史侧的 ``user_message``、以及三种辅助条目的
    ``created_at`` —— 四处各有独立断言,见模块 docstring。
    """
    for turn in golden.turns:
        live = _fold(turn.live)
        replay = _fold(turn.replay)
        history = golden.history[str(turn.run_id)]

        # 先立下界:三条都空时「三者相等」同样通过。
        assert len(live) >= 3, (turn.run_id, live)
        assert len(history) > len(live), (turn.run_id, history)

        assert _content(live) == _content(replay), f"实时 ↔ 回放 漂了(run={turn.run_id})"
        assert _content(replay) == _content(_without_user(history)), (
            f"回放 ↔ 历史 漂了(run={turn.run_id})"
        )


def test_plan_item_settles_on_the_last_snapshot_everywhere(golden: _Golden) -> None:
    """一轮里计划改过两次时,三条路径都必须落在**最后**那份上。

    历史取最后一个 ``plan`` 帧,实时按 ``{run}:plan`` upsert 掉前一份。两边
    只要有一边算错,第三方刷新页面就会看到任务卡从「已完成」跳回「进行中」。
    """
    turn = golden.turns[0]
    live_plan = next(i for i in _fold(turn.live) if i["type"] == "plan")
    replay_plan = next(i for i in _fold(turn.replay) if i["type"] == "plan")
    history_plan = next(i for i in golden.history[str(turn.run_id)] if i["type"] == "plan")

    assert live_plan["steps"] == PLAN_V2["steps"]
    assert replay_plan["steps"] == PLAN_V2["steps"]
    assert history_plan["steps"] == PLAN_V2["steps"]
    # 先证兄弟事实:两份快照真的不一样,上面三条才不是恒真。
    assert PLAN_V1["steps"] != PLAN_V2["steps"]


def test_tool_result_content_is_unwrapped_on_every_path(golden: _Golden) -> None:
    """工具结果的防注入包装在三条路径上都必须已经还原。

    包装是内部表示,条目层的价值正是把它翻译成产品表示;哪条路径漏了还原,
    第三方那条路径上就会看到一段乱码围栏。
    """
    turn = golden.turns[0]
    for label, items in (
        ("live", _fold(turn.live)),
        ("replay", _fold(turn.replay)),
        ("history", golden.history[str(turn.run_id)]),
    ):
        first = next(
            i for i in items if i["type"] == "tool_result" and i.get("call_id") == "call-1"
        )
        assert "北京" in first["content"], label
        assert "expert-work-untrusted" not in first["content"], label
        assert "▁" not in first["content"], label


# ---------------------------------------------------------------------------
# 三处有意不比对的地方 —— 各自的独立断言
# ---------------------------------------------------------------------------


def test_item_ids_are_unique_within_each_path(golden: _Golden) -> None:
    """``id`` 不跨路径承诺,但每条路径**内部**必须唯一 —— 客户端拿它当 key。

    比对时剔除了 ``id``,所以这条是它唯一的守卫:撞 id 意味着两个条目在界面上
    合并成一个,内容静默丢一半。
    """
    for turn in golden.turns:
        for label, items in (
            ("live", _fold(turn.live)),
            ("replay", _fold(turn.replay)),
            ("history", golden.history[str(turn.run_id)]),
        ):
            ids = [i["id"] for i in items]
            assert len(ids) == len(set(ids)), (label, turn.run_id, ids)
            assert all(ids), (label, turn.run_id, ids)


def test_user_message_is_history_only(golden: _Golden) -> None:
    """``user_message`` 只有历史给得出 —— 实时与回放物理上产不出这条。

    它是 graph 的**输入**,躺在检查点里,从没进过事件流(spec §一 第 2 条)。
    这是一处结构性不对称,不是漂移:第三方自己 POST 了那句话,实时侧本来就
    知道;刷新之后由服务端补上。比对时从历史侧剔除,由这一条正面钉住 ——
    哪天实时侧真开始发 user_message 了,这里会红,提醒去掉那处剔除。
    """
    for turn in golden.turns:
        history = golden.history[str(turn.run_id)]
        assert _types(history).count(_USER_MESSAGE) == 1, (turn.run_id, _types(history))
        assert history[0]["type"] == _USER_MESSAGE, history[0]
        assert _USER_MESSAGE not in _types(_fold(turn.live))
        assert _USER_MESSAGE not in _types(_fold(turn.replay))


def test_aux_created_at_replay_equals_history_exactly(golden: _Golden) -> None:
    """回放与历史的辅助条目 ``created_at`` **逐字节相等** —— 没有容差。

    两边取的是同一行 ``run_event`` 的落库时刻(``make_event_record`` 用同一个
    ``created_at_ms`` 同时算出 ``created_at_ms`` 与 ``created_at``),所以这一
    对上任何差异都是真 bug,不是采样抖动。
    """
    seen = 0
    for turn in golden.turns:
        replay = _aux_times(_fold(turn.replay))
        history = _aux_times(golden.history[str(turn.run_id)])
        assert replay == history, (turn.run_id, replay, history)
        # 先证兄弟事实:真的取到了非空时刻,否则 ``None == None`` 恒真。
        assert all(t is not None for _k, t in replay), (turn.run_id, replay)
        seen += len(replay)
    assert seen >= 3, seen


def test_live_aux_created_at_matches_the_stored_clock(golden: _Golden) -> None:
    """实时侧的辅助条目 ``created_at`` 与落库时刻同一瞬间(容差内)。

    实时没有落库时刻可用,取的是 bridge ``publish`` 时 ``id:`` 里的发布时钟;
    ``_enqueue_event`` 紧接着又取一次 ``time.time()``。原则上是两次采样,实践
    中同毫秒 —— 所以这里只对这一对放宽,回放 ↔ 历史那一对照旧逐字节相等。
    """
    compared = 0
    for turn in golden.turns:
        live = _aux_times(_fold(turn.live))
        stored = _aux_times(_fold(turn.replay))
        assert [k for k, _t in live] == [k for k, _t in stored], (turn.run_id, live, stored)
        for (kind, live_at), (_k, stored_at) in zip(live, stored, strict=True):
            assert live_at is not None and stored_at is not None, (turn.run_id, kind)
            drift = abs(_ms(live_at) - _ms(stored_at))
            assert drift <= _AUX_CLOCK_TOLERANCE_MS, (turn.run_id, kind, live_at, stored_at)
            compared += 1
    assert compared >= 3, compared


def test_live_aux_created_at_is_the_frame_clock(golden: _Golden) -> None:
    """堵上容差的盲区:实时的 ``created_at`` 必须是**帧的**时刻,不是转发时的 now。

    上一条的容差对「转发那一刻取一次 ``now``」这类实现不敏感 —— 转发紧跟着
    发布,差值同样落在容差内。这里换一个判据:fixture 在 run 全部结束之后才
    记下 ``settled_at``,又睡了一下才开始消费实时流,所以取「转发时的 now」
    一定 **晚于** ``settled_at``,而取帧时刻一定 **早于**它。
    """
    checked = 0
    for turn in golden.turns:
        for kind, live_at in _aux_times(_fold(turn.live)):
            assert live_at is not None, (turn.run_id, kind)
            assert _ms(live_at) <= golden.settled_at * 1000.0, (
                f"{kind} 的 created_at 晚于 run 结束时刻 —— 像是在转发时取的 now",
                turn.run_id,
                live_at,
            )
            checked += 1
    assert checked >= 3, checked


def test_message_created_at_is_byte_identical_on_all_three_paths(golden: _Golden) -> None:
    """消息类条目的 ``created_at`` 三条路径逐字节相等 —— 它不在放宽范围内。

    戳是写入侧盖进 ``additional_kwargs`` 的,三条路径读的是同一份数据,没有
    第二次采样。这条把「放宽只发给三种辅助条目」这句话钉死:谁把容差扩大到
    消息类条目上,这里不会红,但 :func:`test_three_paths_agree_on_item_content`
    会 —— 因为消息类的 ``created_at`` 根本没被 :func:`_content` 摘出去。
    """
    turn = golden.turns[0]
    live = [i["created_at"] for i in _fold(turn.live) if i["type"] == "assistant_message"]
    history = [
        i["created_at"]
        for i in golden.history[str(turn.run_id)]
        if i["type"] == "assistant_message"
    ]
    assert live == history
    assert live == [(T0 + timedelta(seconds=n)).isoformat() for n in (1, 2, 3)], live


def test_worker_frames_pass_through_untouched_on_both_stream_paths(golden: _Golden) -> None:
    """``worker`` 帧在两条流式路径上**原样透传**,不被转换成条目。

    spec §五「修正 —— ``worker`` 的处置(拍板)」:转成 ``ToolCallItem.worker``
    就得等子任务的 ``end`` 帧才能发工具卡的 ``item.done``,时机语义会很别扭,
    所以实时与回放继续发独立的 ``worker`` 事件。

    这条同时是下面那条否定断言的**非空前提** —— 黄金 run 真的委托了一次子任务
    (``tools1`` 节点经生产的帧构建函数发了 start / end 两帧)。没有它,
    「实时不填 worker」只是因为压根没有 worker 可填,断言恒真。
    """
    turn = golden.turns[0]
    for label, frames in (("live", turn.live), ("replay", turn.replay)):
        worker_frames = [d for _fid, name, d in frames if name == "worker"]
        assert len(worker_frames) == 2, (label, worker_frames)
        assert {d["kind"] for d in worker_frames} == {"start", "end"}, (label, worker_frames)
        # 挂载键是 LangChain 的 ``tool_call_id``,与 ``ai.tool_calls[].id`` 同值。
        assert {d["parent_tool_call_id"] for d in worker_frames} == {DELEGATED_CALL_ID}, label


def test_worker_never_leaks_into_live_or_replay(golden: _Golden) -> None:
    """``tool_call.worker`` 在实时与回放上**恒缺席** —— spec §五 的不同构方向。

    这是 spec 拍板的「唯一不完全同构处」:``worker`` 只在**历史**里填(从落库
    的 worker 帧按 ``parent_tool_call_id`` 重建),两条流式路径不填。所以
    :func:`_content` 把 ``worker`` 和 ``id`` 一起排除;这一条负责钉住排除是
    **有方向的**,不是两边都随便。

    上一条测试保证了这个 run 真的产生过 worker 帧,所以下面不是空集恒真。

    **已知缺口(有意留给 PR5)**:这里没有断言历史侧**确实填了** ``worker``。
    回填的实现落在 ``api/external_session_items.py``,那是并行 PR5 的文件,本
    分支上还没有它 —— 现在写正向断言会让本 PR 红着合。PR5 落地时应当在**它
    自己的** PR 里补上「历史有」那一半,和这里的「流式没有」合起来才是完整
    契约。在那之前,这个不对称只被测到了一个方向 —— 写在这里,免得它悄悄
    不存在。
    """
    checked = 0
    for turn in golden.turns:
        for label, items in (("live", _fold(turn.live)), ("replay", _fold(turn.replay))):
            calls = [i for i in items if i["type"] == "tool_call"]
            assert all("worker" not in c for c in calls), (label, turn.run_id, calls)
            checked += len(calls)
    # 先证兄弟事实:真的有工具调用条目被检查过,否则 ``all(...)`` 在空集上恒真。
    assert checked >= 8, checked


# ---------------------------------------------------------------------------
# 事件序列的不对称是设计,不是 bug —— 但实时那三段必须真的在
# ---------------------------------------------------------------------------


def test_live_really_streamed_while_replay_only_settles(golden: _Golden) -> None:
    """实时有 ``added`` → ``delta`` → ``done`` 三段,回放只有 ``done``。

    没有这一条,「三者相等」在实时路径退化成回放(转换器不发 delta 了)时
    照样全绿 —— 那正是「另外两条根本没被真正驱动」的形态。
    """
    turn = golden.turns[0]
    live_names = [name for _fid, name, _d in turn.live]
    assert ITEM_ADDED in live_names
    assert ITEM_DELTA in live_names
    assert ITEM_DONE in live_names

    replay_names = [name for _fid, name, _d in turn.replay]
    # 先证兄弟事件在:回放确实产出了条目,下面两条否定断言才不是空转。
    assert replay_names.count(ITEM_DONE) >= 3, replay_names
    assert ITEM_ADDED not in replay_names
    assert ITEM_DELTA not in replay_names

    # 每条 delta 都要落到一个真的条目上,否则打字机文本挂在孤儿 id 上。
    settled = {i["id"] for i in _fold(turn.live)}
    delta_ids = {d["id"] for _fid, name, d in turn.live if name == ITEM_DELTA}
    assert delta_ids, turn.live
    assert delta_ids <= settled, (delta_ids, settled)


def test_item_delta_carries_no_seq(golden: _Golden) -> None:
    """``item.delta`` 不带 ``id:`` 行(spec §五 硬约束 (b))。

    token 帧是 ephemeral 的,不落库、不占序号。让不可回放的帧占用 seq,客户端
    解析出的续传位点就会跑到 ``since_seq`` 实际能回放的范围之外,断线重连
    **静默漏事件**。
    """
    turn = golden.turns[0]
    deltas = [(fid, name) for fid, name, _d in turn.live if name == ITEM_DELTA]
    assert deltas, turn.live
    assert all(fid is None for fid, _name in deltas), deltas
