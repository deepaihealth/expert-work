"""黄金 run 的图与素材 —— 供 ``test_conversation_items_parity.py`` 驱动。

拆出来的理由与 ``agent_fixtures.py`` / ``auth_fixtures.py`` 同款:被测的是
**断言**,这里只是把「跑什么样的一轮」摆出来。三张图都是真的 LangGraph 图,
编译时挂上会话共用的那个 checkpointer —— 所以 ``updates`` 帧是 LangGraph 自己
产出的,检查点消息是图自己写的,没有一条是测试手工塞进去的。

节点是脚本化的(没有真 LLM),但从节点返回值往后的整条管道全是生产代码。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from control_plane.settings import Settings
from expert_work.common.message_stamp import stamp_message
from expert_work.common.spotlight import spotlight_untrusted
from orchestrator.graph_builder._config import TOKEN_SINK_KEY
from orchestrator.tools._worker_events import (
    WORKER_EVENT_SINK_KEY,
    WorkerIdentity,
    build_worker_end_frame,
    build_worker_start_frame,
)
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER

#: 消息戳上的基准时刻。戳是写入侧盖的,三条路径读的是同一份
#: ``additional_kwargs``,所以**消息类**条目的 ``created_at`` 必须逐字节相等。
T0 = datetime(2026, 8, 25, 9, 0, 0, tzinfo=UTC)

NONCE = "n0nce-parity"

#: 委托出去的那个子任务挂在哪次工具调用上。历史侧重建 ``tool_call.worker``
#: 时按 ``parent_tool_call_id`` 找的就是它(spec §五)。
DELEGATED_CALL_ID = "call-1"
WORKER_ID = "w-parity-1"

PLAN_V1: dict[str, Any] = {
    "goal": "查两地天气并换算",
    "steps": [{"title": "查北京", "status": "pending"}, {"title": "查上海", "status": "pending"}],
}
#: 同一轮里计划改过第二次 —— 历史取**最后一个** plan 帧,实时按 ``{run}:plan``
#: upsert 掉前一份。两边都必须落到 V2 上,这是 spec §三「一轮里计划可能改多次,
#: 历史只保留最后一次」在三条路径上的实证。
PLAN_V2: dict[str, Any] = {
    "goal": "查两地天气并换算",
    "steps": [{"title": "查北京", "status": "done"}, {"title": "查上海", "status": "done"}],
}

AGENT_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


class State(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    step_count: int
    plan: Any
    pending_approval: Any


def build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def stamp(msg: BaseMessage, *, run_id: UUID, secs: int) -> BaseMessage:
    return stamp_message(msg, run_id=str(run_id), now=T0 + timedelta(seconds=secs))


def _tool_msg(
    *,
    call_id: str,
    name: str,
    content: str,
    duration_ms: int,
    status: str = "success",
    artifact: Any = None,
) -> ToolMessage:
    return ToolMessage(
        content=content,
        tool_call_id=call_id,
        name=name,
        status=status,
        artifact=artifact,
        additional_kwargs={"duration_ms": duration_ms},
    )


def _worker_identity() -> WorkerIdentity:
    """一次深度 1 的子任务委托 —— 挂在 :data:`DELEGATED_CALL_ID` 那次调用上。

    ``parent_tool_call_id`` 就是 LangChain 的 ``tool_call_id``,与
    ``updates`` 里 ``ai.tool_calls[].id`` 同值 —— 历史侧靠它把 worker 挂回
    工具卡(spec §五)。
    """
    return WorkerIdentity(
        worker_id=WORKER_ID,
        parent_worker_id=None,
        parent_tool_call_id=DELEGATED_CALL_ID,
        label="查北京天气",
        agent_ref="weather-bot",
        depth=1,
    )


def multi_step_graph(run_id: UUID, checkpointer: InMemorySaver) -> Any:
    """黄金 run 的图 —— spec §四 点名的那个形状。

    用户提问 → 助手说一句中间说明并发起工具调用 → 工具返回 → 助手**再**发一次
    工具调用(一条 AIMessage 带**两个**调用,覆盖 spec §八 的并行分支)→ 两个
    工具结果 → 助手给最终答案。外加两份 ``plan`` 快照,以及一次真的子任务委托
    (``worker`` 帧,挂在 :data:`DELEGATED_CALL_ID` 上)。

    只跑一问一答的话,实时侧对 ``channel`` 的局部判定(无 tool_calls ⟹ final)
    恰好与历史一致,测试会全绿而 bug 仍在 —— 这是 spec §四 专门点名的
    「修复自带的测试给坏版本发合格证」那一类。
    """
    a1 = stamp(
        AIMessage(
            content="我先查一下北京的天气。",
            tool_calls=[
                {
                    "id": DELEGATED_CALL_ID,
                    "name": "search",
                    "args": {"q": "北京"},
                    "type": "tool_call",
                }
            ],
        ),
        run_id=run_id,
        secs=1,
    )
    t1 = _tool_msg(
        call_id=DELEGATED_CALL_ID,
        name="search",
        # 真实工具结果带防注入包装 —— 条目层要给**还原后**的文本。
        content=spotlight_untrusted("北京 25 摄氏度", nonce=NONCE),
        duration_ms=41,
    )
    a2 = stamp(
        AIMessage(
            content="上海也查一下,同时把 25 摄氏度换算成华氏。",
            tool_calls=[
                {"id": "call-2", "name": "search", "args": {"q": "上海"}, "type": "tool_call"},
                {"id": "call-3", "name": "convert_temp", "args": {"c": 25}, "type": "tool_call"},
            ],
        ),
        run_id=run_id,
        secs=2,
    )
    t2 = _tool_msg(
        call_id="call-2",
        name="search",
        content=spotlight_untrusted("上海 28 摄氏度", nonce=NONCE),
        duration_ms=55,
    )
    t3 = _tool_msg(
        call_id="call-3",
        name="convert_temp",
        content="换算服务超时",
        duration_ms=3,
        status="error",
        artifact={"unit": "F"},
    )
    a3 = stamp(AIMessage(content="北京 25°C,上海 28°C;华氏换算失败了。"), run_id=run_id, secs=3)

    graph = StateGraph(State)

    async def planner(_s: State) -> dict[str, Any]:
        return {"plan": deepcopy(PLAN_V1)}

    async def agent1(_s: State, config: RunnableConfig) -> dict[str, Any]:
        sink = (config.get("configurable") or {}).get(TOKEN_SINK_KEY)
        if sink is not None:
            # 打字机预览 + 工具卡预览。``step`` 必须等于本节点将要写回的
            # ``step_count`` —— 实时条目 id 是 ``{run}:step:{step_count}``。
            await sink({"step": 1, "channel": "content", "text": "我先查一下"})
            await sink({"step": 1, "channel": "content", "text": "北京的天气。"})
            await sink(
                {
                    "step": 1,
                    "channel": "tool_args",
                    "call_id": DELEGATED_CALL_ID,
                    "name": "search",
                }
            )
        return {"messages": [a1], "step_count": 1}

    async def tools1(_s: State, config: RunnableConfig) -> dict[str, Any]:
        # 真的把这次工具调用委托出去 —— 用生产的帧构建函数,帧形态与
        # ``spawn_worker`` 发出来的完全一致。
        worker_sink = (config.get("configurable") or {}).get(WORKER_EVENT_SINK_KEY)
        if worker_sink is not None:
            ident = _worker_identity()
            await worker_sink(
                build_worker_start_frame(
                    ident, wseq=0, task="查北京天气", role="researcher", max_steps=4
                )
            )
            await worker_sink(
                build_worker_end_frame(
                    ident,
                    wseq=1,
                    outcome="success",
                    iteration_used=2,
                    llm_call_count=2,
                    wall_clock_ms=37,
                )
            )
        return {"messages": [t1]}

    async def agent2(_s: State) -> dict[str, Any]:
        return {"messages": [a2], "step_count": 2}

    async def tools2(_s: State) -> dict[str, Any]:
        # 计划在这一轮里改了第二次(``update_plan`` 工具的真实形态)。
        return {"messages": [t2, t3], "plan": deepcopy(PLAN_V2)}

    async def agent3(_s: State, config: RunnableConfig) -> dict[str, Any]:
        sink = (config.get("configurable") or {}).get(TOKEN_SINK_KEY)
        if sink is not None:
            await sink({"step": 3, "channel": "content", "text": "北京 25°C,"})
        return {"messages": [a3], "step_count": 3}

    graph.add_node("planner", planner)
    graph.add_node("agent1", agent1)
    graph.add_node("tools1", tools1)
    graph.add_node("agent2", agent2)
    graph.add_node("tools2", tools2)
    graph.add_node("agent3", agent3)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "agent1")
    graph.add_edge("agent1", "tools1")
    graph.add_edge("tools1", "agent2")
    graph.add_edge("agent2", "tools2")
    graph.add_edge("tools2", "agent3")
    return graph.compile(checkpointer=checkpointer)


def approval_graph(run_id: UUID, checkpointer: InMemorySaver) -> Any:
    """停在审批闸上的一轮 —— 覆盖 ``approval`` 条目。"""
    a4 = stamp(
        AIMessage(
            content="这一步要发邮件给客户,需要你确认。",
            tool_calls=[
                {"id": "call-4", "name": "send_email", "args": {"to": "客户"}, "type": "tool_call"}
            ],
        ),
        run_id=run_id,
        secs=11,
    )
    requested = T0 + timedelta(seconds=11)
    request = {
        "request_id": "approval:parity",
        "node": "tools",
        "reason_kind": "policy_gate",
        "action_summary": "approval-gated tool 'send_email'",
        "proposed_args": {"to": "客户"},
        "requested_at": requested.isoformat(),
        "timeout_at": (requested + timedelta(hours=24)).isoformat(),
        "binding_digest": "",
    }

    graph = StateGraph(State)

    async def agent4(_s: State) -> dict[str, Any]:
        return {"messages": [a4], "step_count": 1}

    async def gate(_s: State) -> dict[str, Any]:
        return {"pending_approval": deepcopy(request)}

    graph.add_node("agent4", agent4)
    graph.add_node("gate", gate)
    graph.add_edge(START, "agent4")
    graph.add_edge("agent4", "gate")
    return graph.compile(checkpointer=checkpointer)


def failing_graph(run_id: UUID, checkpointer: InMemorySaver) -> Any:
    """跑了一半才失败的一轮 —— 覆盖 ``error`` 条目排在已产出内容之后。"""
    a5 = stamp(AIMessage(content="这就重算一次。"), run_id=run_id, secs=21)

    graph = StateGraph(State)

    async def agent5(_s: State) -> dict[str, Any]:
        return {"messages": [a5], "step_count": 1}

    async def boom(_s: State) -> dict[str, Any]:
        # ``RuntimeError`` 不在 ``TRANSIENT_RUN_ERRORS`` 里,所以不会触发
        # run 级重试(重试会把消息重放一遍,那是另一个题目)。
        msg = "模型返回不可解析"
        raise RuntimeError(msg)

    graph.add_node("agent5", agent5)
    graph.add_node("boom", boom)
    graph.add_edge(START, "agent5")
    graph.add_edge("agent5", "boom")
    return graph.compile(checkpointer=checkpointer)
