"""B-226 ①② —— 撞预算的收尾指令带平台记录, 步数快用完时先提醒。

测试环境实测(10-28):49 步撞 48 步上限, 收尾指令说「写出你最好的、完整的最终答复」, 模型
答「交付文件已通过逐页渲染核验」, 而这一轮一次 ``save_artifact`` 都没调过。收尾指令改成
平台记下的事实(登记了几个产物、几次失败没补上、计划还有几步没完成), 文字固定, 只有数字
与上限是动态的;它落进检查点, 下一轮看得出这条回复是被迫收尾写的。步数进入最后一段时,
每一步的提示词尾部附一条步数预警(不落库), 本 run 只发一次 ``warning`` 帧。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import HIDE_FROM_UI
from expert_work.protocol import Plan
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import (
    CallNext,
    LoopDetectionMiddleware,
    MiddlewareChain,
    MiddlewareContext,
)
from orchestrator import AgentState, GraphRunner, ToolRegistry, build_react_graph
from orchestrator.graph_builder.builder import build_wrapup_instruction, step_budget_notice
from orchestrator.graph_builder.platform_context import PLATFORM_CONTEXT_OPEN
from orchestrator.llm.router import AllProvidersExhaustedError
from orchestrator.tools._child_run import _child_config
from orchestrator.tools._guards import GUARD_SINK_KEY, TOKEN_BUDGET_KEY, TokenBudget
from orchestrator.tools.artifact import (
    ARTIFACT_RECORDER_KEY,
    SAVE_ARTIFACT_TOOL_NAME,
    ArtifactRecorder,
)
from orchestrator.tools.error_classifier import ClassifiedToolError
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec

_WRAPUP_TAG = "[budget notice]"
_STEP_TAG = "[step budget notice]"


@dataclass
class _LLM:
    responses: list[AIMessage]
    seen: list[list[BaseMessage]] = field(default_factory=list)
    tool_counts: list[int] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        self.seen.append(list(messages))
        self.tool_counts.append(len(list(tools)))
        return self.responses[min(len(self.seen) - 1, len(self.responses) - 1)]


@dataclass
class _Tool:
    name: str = "probe"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.name)

    async def call(self, args: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _registry(*, saves: bool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Tool())
    if saves:
        registry.register(_Tool(name=SAVE_ARTIFACT_TOOL_NAME))
    return registry


def _call(call_id: str) -> AIMessage:
    tc = {"name": "probe", "args": {"i": call_id}, "id": call_id, "type": "tool_call"}
    return AIMessage(content="", tool_calls=[tc])


def _failure() -> ClassifiedToolError:
    return ClassifiedToolError(
        tool_name="write_file",
        error_class="permission_denied",
        summary="x",
        retryable=False,
        advice="y",
    )


def _plan(*statuses: str) -> Plan:
    return Plan.model_validate(
        {
            "goal": "g",
            "steps": [
                {"id": str(i), "description": f"s{i}", "status": s}
                for i, s in enumerate(statuses, start=1)
            ],
        }
    )


def _middleware() -> MiddlewareChain:
    return MiddlewareChain.from_middlewares("after_llm_call", [LoopDetectionMiddleware()])


async def _run(
    llm: _LLM,
    payload: dict[str, Any],
    *,
    saves: bool = True,
    configurable: dict[str, Any] | None = None,
    with_middleware: bool = False,
    thread_id: str | None = None,
    checkpointer: Any = None,
) -> AgentState:
    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=_registry(saves=saves),
        after_llm_chain=_middleware() if with_middleware else None,
    )
    cfg: RunnableConfig = {
        "configurable": {"thread_id": thread_id or str(uuid4()), **(configurable or {})}
    }
    if checkpointer is not None:
        return await GraphRunner(checkpointer=checkpointer).compile(graph).ainvoke(payload, cfg)
    async with make_checkpointer("memory") as cp:
        return await GraphRunner(checkpointer=cp).compile(graph).ainvoke(payload, cfg)


def _sink() -> tuple[list[dict[str, Any]], Any]:
    seen: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        seen.append(frame)

    return seen, _publish


def _text(msg: BaseMessage) -> str:
    return str(msg.content)


# ------------------------------------------------------------ ① the instruction text


@pytest.mark.parametrize(
    ("reason", "named", "not_named"),
    [
        ("max_steps", "step budget (48 steps)", "no-progress"),
        ("token_budget", "token budget", "step budget"),
        (
            "no_progress",
            "no-progress limit (the same tool calls kept repeating without progress)",
            "step budget",
        ),
    ],
)
def test_wrapup_names_the_limit_of_the_exit_reason(reason: str, named: str, not_named: str) -> None:
    text = build_wrapup_instruction(
        reason=reason, max_steps=48, unresolved_failures=0, artifacts=0, open_plan_steps=None
    )
    assert text.startswith(_WRAPUP_TAG)
    assert f"reached its {named} and tools are no longer available" in text
    assert not_named not in text


def test_wrapup_states_the_platform_record_and_no_completion_contract() -> None:
    text = build_wrapup_instruction(
        reason="max_steps", max_steps=48, unresolved_failures=2, artifacts=0, open_plan_steps=3
    )
    assert (
        "Platform record for this run: 0 deliverable(s) registered with save_artifact; "
        "2 tool failure(s) not resolved by a later successful call; "
        "3 plan step(s) not completed."
    ) in text
    assert "Only files registered with save_artifact in this run have been delivered." in text
    assert "say which parts of the request are finished, which are not" in text
    # L7 —— 这条落库、下一轮也看得到:只禁本 run / 这条回复的工具, 「继续」那一轮照常有工具。
    assert "tools are no longer available for the rest of this run." in text
    assert text.endswith("Do not call tools in this reply.")
    # 旧措辞要模型「写出最好的、完整的最终答复」, 盖过了完成度约定。
    assert "complete final answer" not in text


def test_wrapup_omits_clauses_for_capabilities_the_run_does_not_have() -> None:
    text = build_wrapup_instruction(
        reason="max_steps", max_steps=6, unresolved_failures=1, artifacts=None, open_plan_steps=None
    )
    assert "save_artifact" not in text and "deliverable" not in text
    assert "plan step" not in text
    assert "Platform record for this run: 1 tool failure(s) not resolved" in text
    assert "Work not checked after its last change is unchecked." in text


# ------------------------------------------------------------ ① wiring in agent_node


@pytest.mark.asyncio
async def test_wrapup_prompt_carries_counts_from_state_plan_and_recorder() -> None:
    recorder = ArtifactRecorder()
    recorder({"name": "a.md"})
    llm = _LLM([AIMessage(content="partial")])
    await _run(
        llm,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 3,
            "max_steps": 3,
            "plan": _plan("completed", "in_progress", "pending"),
            "unresolved_failures": [_failure()],
        },
        configurable={ARTIFACT_RECORDER_KEY: recorder},
    )
    wrapup = llm.seen[0][-1]
    assert isinstance(wrapup, HumanMessage) and wrapup.additional_kwargs.get(HIDE_FROM_UI)
    text = _text(wrapup)
    assert "reached its step budget (3 steps)" in text
    assert "1 deliverable(s) registered with save_artifact" in text
    assert "1 tool failure(s) not resolved" in text
    assert "2 plan step(s) not completed" in text
    assert llm.tool_counts == [0]


@pytest.mark.asyncio
async def test_wrapup_follows_the_platform_block_and_is_kept_clean_of_it() -> None:
    """B-226 ① / B-162 —— 收尾指令排在平台段之后:平台段照旧贴在最后一条工具结果 / 用户消息上,
    收尾指令是最后一条、字节与落库的那条一致(不夹着计划 / 工作区快照)。"""
    llm = _LLM([AIMessage(content="partial")])
    state = await _run(
        llm,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 2,
            "max_steps": 2,
            "plan": _plan("in_progress"),
        },
    )
    *_, carrier, wrapup = llm.seen[0]
    assert PLATFORM_CONTEXT_OPEN in _text(carrier) and _text(carrier).startswith("go")
    assert _text(wrapup).startswith(_WRAPUP_TAG) and PLATFORM_CONTEXT_OPEN not in _text(wrapup)
    persisted = next(m for m in state["messages"] if _text(m).startswith(_WRAPUP_TAG))
    assert persisted.content == wrapup.content


@pytest.mark.asyncio
async def test_a_childs_wrapup_counts_only_what_the_child_registered() -> None:
    """B-226 ① —— 静态子 Agent(绑了 save_artifact)撞上限:父已登记 2 个, 子代的收尾里说 0 个;
    子代登记的仍进本 run 的同一份清单。"""
    parent = ArtifactRecorder()
    parent({"name": "a.md"})
    parent({"name": "b.md"})
    child_cfg = _child_config(
        ToolContext(tenant_id=uuid4(), artifact_recorder=parent),
        sub_thread_id=uuid4(),
        sub_run_id=uuid4(),
    )
    child_recorder = child_cfg["configurable"][ARTIFACT_RECORDER_KEY]
    assert isinstance(child_recorder, ArtifactRecorder) and child_recorder is not parent
    llm = _LLM([AIMessage(content="partial")])
    await _run(
        llm,
        {"messages": [HumanMessage(content="sub task")], "step_count": 3, "max_steps": 3},
        configurable={k: v for k, v in child_cfg["configurable"].items() if k != "thread_id"},
    )
    assert "0 deliverable(s) registered with save_artifact" in _text(llm.seen[0][-1])
    child_recorder({"name": "c.md"})
    assert [e["name"] for e in parent.snapshot()] == ["a.md", "b.md", "c.md"]
    # 父的收尾数整棵子树:自己的 2 个 + 委派出去的子代登记的 1 个。
    parent_llm = _LLM([AIMessage(content="partial")])
    await _run(
        parent_llm,
        {"messages": [HumanMessage(content="go")], "step_count": 3, "max_steps": 3},
        configurable={ARTIFACT_RECORDER_KEY: parent},
    )
    assert "3 deliverable(s) registered with save_artifact" in _text(parent_llm.seen[0][-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saves", "recorded"),
    [(False, True), (True, False)],
    ids=["save_artifact-unbound", "no-recorder"],
)
async def test_wrapup_drops_the_registration_clause_when_it_cannot_be_stated(
    saves: bool, recorded: bool
) -> None:
    llm = _LLM([AIMessage(content="partial")])
    await _run(
        llm,
        {"messages": [HumanMessage(content="go")], "step_count": 1, "max_steps": 1},
        saves=saves,
        configurable={ARTIFACT_RECORDER_KEY: ArtifactRecorder()} if recorded else None,
    )
    text = _text(llm.seen[0][-1])
    assert text.startswith(_WRAPUP_TAG)
    assert "save_artifact" not in text
    assert "plan step" not in text


@pytest.mark.asyncio
async def test_stuck_wrapup_says_no_progress_not_step_budget() -> None:
    llm = _LLM([AIMessage(content="stuck")])
    state = await _run(
        llm,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "loop_trip_count": 2,
            "max_no_progress": 2,
        },
    )
    text = _text(llm.seen[0][-1])
    assert "no-progress limit" in text and "step budget" not in text
    assert state["exit_reason"] == "no_progress"


@pytest.mark.asyncio
async def test_steps_and_tokens_both_spent_text_agrees_with_exit_reason() -> None:
    """两个都撞了:``exit_reason`` 记 ``max_steps``, 收尾文字也说步数(旧逻辑按 token 说)。"""
    llm = _LLM([AIMessage(content="out of both")])
    state = await _run(
        llm,
        {"messages": [HumanMessage(content="go")], "step_count": 4, "max_steps": 4},
        configurable={TOKEN_BUDGET_KEY: TokenBudget(limit=10, spent=20)},
    )
    text = _text(llm.seen[0][-1])
    assert "reached its step budget (4 steps)" in text and "token budget" not in text
    assert state["exit_reason"] == "max_steps"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_middleware", [False, True], ids=["plain", "middleware"])
async def test_wrapup_instruction_is_persisted_and_seen_next_turn(with_middleware: bool) -> None:
    first = _LLM([AIMessage(content="partial answer")])
    thread = str(uuid4())
    async with make_checkpointer("memory") as cp:
        state = await _run(
            first,
            {"messages": [HumanMessage(content="go")], "step_count": 2, "max_steps": 2},
            with_middleware=with_middleware,
            thread_id=thread,
            checkpointer=cp,
        )
        *_, wrapup, reply = state["messages"]
        assert isinstance(wrapup, HumanMessage) and _text(wrapup).startswith(_WRAPUP_TAG)
        assert wrapup.additional_kwargs.get(HIDE_FROM_UI) is True
        assert isinstance(reply, AIMessage) and reply.content == "partial answer"
        # 下一轮:模型看得到这条回复是撞预算之后被要求收尾写的。
        second = _LLM([AIMessage(content="next")])
        await _run(
            second,
            {"messages": [HumanMessage(content="continue")], "step_count": 0},
            with_middleware=with_middleware,
            thread_id=thread,
            checkpointer=cp,
        )
    prompt = second.seen[0]
    idx = next(i for i, m in enumerate(prompt) if _text(m).startswith(_WRAPUP_TAG))
    assert prompt[idx + 1].content == "partial answer"


# ------------------------------------------------------------ ② step-budget notice


@pytest.mark.parametrize(
    ("step_count", "max_steps", "expected"),
    [
        (40, 48, "40 of 48 steps used in this run; 8 left."),
        (39, 48, None),
        (4, 6, "4 of 6 steps used in this run; 2 left."),
        (3, 6, None),
        (5, 6, "5 of 6 steps used in this run; 1 left."),
        (6, 6, None),
        (0, 0, None),
    ],
)
def test_step_notice_window(step_count: int, max_steps: int, expected: str | None) -> None:
    text = step_budget_notice(step_count=step_count, max_steps=max_steps, registers=True)
    if expected is None:
        assert text is None
    else:
        assert text is not None and text.startswith(f"{_STEP_TAG} {expected}")


_REGISTER_NOW = (
    "Register finished files that are not registered yet, or have changed since you registered "
    "them, with save_artifact now (unless the user asked otherwise), so they are delivered even "
    "if the budget runs out."
)
_CONTINUE = "Continue the task; do not stop or shorten the work solely because of this notice."


def test_step_notice_is_a_checkpoint_not_a_call_to_stop() -> None:
    """B-226 ② —— 预警只说「先把做完的登记好, 再接着做」, 不催收尾。hermes-agent 把
    「还剩 N 步, 赶紧收尾」的压力提醒删掉了(c8aff74632, #7915):它让模型过早放弃复杂任务;
    现行的 opt-in 版本是「先存下进度, 再继续;别只因为这条提醒就停」。只让登记还没登记的 /
    改过的(不是每一步都重登一遍), 用户另有交代(如「中途别登记」)时听用户的。"""
    bound = step_budget_notice(step_count=5, max_steps=6, registers=True)
    unbound = step_budget_notice(step_count=5, max_steps=6, registers=False)
    head = f"{_STEP_TAG} 5 of 6 steps used in this run; 1 left."
    assert bound == f"{head} {_REGISTER_NOW} {_CONTINUE}"
    # 没绑 save_artifact:不提存档, 只有步数和「接着做」。
    assert unbound == f"{head} {_CONTINUE}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("saves", "wording"), [(True, _REGISTER_NOW), (False, _CONTINUE)])
async def test_step_notice_in_the_prompt_follows_the_save_artifact_binding(
    saves: bool, wording: str
) -> None:
    llm = _LLM([AIMessage(content="done")])
    await _run(
        llm,
        {"messages": [HumanMessage(content="go")], "step_count": 5, "max_steps": 6},
        saves=saves,
    )
    tail = _platform_block(llm.seen[0][-1])
    assert _STEP_TAG in tail and wording in tail


def _platform_block(msg: BaseMessage) -> str:
    """最后一条消息末尾那段 ``<platform-context>``(B-162);没有就是空串。"""
    text = _text(msg)
    start = text.find(PLATFORM_CONTEXT_OPEN)
    return text[start:] if start >= 0 else ""


@pytest.mark.asyncio
@pytest.mark.parametrize("with_middleware", [False, True], ids=["plain", "middleware"])
async def test_step_notice_rides_the_last_steps_only_and_is_not_persisted(
    with_middleware: bool,
) -> None:
    """B-226 ② / B-162 —— 预警是平台段:进 ``<platform-context>``、贴在最后一条消息(这里是
    最后一条工具结果)末尾, 不另起一条 user 消息, 也不落库。"""
    llm = _LLM([*(_call(f"c{i}") for i in range(6)), AIMessage(content="wrapped")])
    events, publish = _sink()
    state = await _run(
        llm,
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 6},
        configurable={GUARD_SINK_KEY: publish},
        with_middleware=with_middleware,
    )
    blocks = [_platform_block(prompt[-1]) for prompt in llm.seen]
    assert [_STEP_TAG in b for b in blocks] == [False] * 4 + [True, True, False]
    assert f"{_STEP_TAG} 4 of 6 steps used in this run; 2 left." in blocks[4]
    assert f"{_STEP_TAG} 5 of 6 steps used in this run; 1 left." in blocks[5]
    for prompt in llm.seen[4:6]:
        assert isinstance(prompt[-1], ToolMessage)
        assert sum(_STEP_TAG in _text(m) for m in prompt) == 1
    assert _text(llm.seen[6][-1]).startswith(_WRAPUP_TAG)
    assert not any(_STEP_TAG in _text(m) for m in llm.seen[6])
    assert not any(_STEP_TAG in _text(m) for m in state["messages"])
    # 进入窗口时一条 warning, 撞上限时一条 tripped。
    assert events == [
        {"kind": "warning", "guard": "max_steps", "detail": {"steps": 4, "max": 6}},
        {"kind": "tripped", "guard": "max_steps", "detail": {"steps": 6, "max": 6}},
    ]
    assert state["step_budget_warned"] is True


@pytest.mark.asyncio
async def test_step_warning_frame_is_not_repeated_once_the_run_warned() -> None:
    """一轮里只发一次:续跑段(审批后)从检查点带着 ``step_budget_warned`` 接着跑。"""
    llm = _LLM([_call("c0"), AIMessage(content="done")])
    events, publish = _sink()
    await _run(
        llm,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 4,
            "max_steps": 6,
            "step_budget_warned": True,
        },
        configurable={GUARD_SINK_KEY: publish},
    )
    assert _STEP_TAG in _platform_block(llm.seen[0][-1])
    assert events == []


_TOKEN_NOTICE_90 = (
    "[token budget notice] This run has used 90 of its 100 token budget (10 remaining). "
    "Converge quickly: prefer finishing with what you have over further tool exploration."
)


@pytest.mark.asyncio
async def test_step_and_token_notices_ride_one_platform_context_block() -> None:
    llm = _LLM([AIMessage(content="done")])
    await _run(
        llm,
        {"messages": [HumanMessage(content="go")], "step_count": 5, "max_steps": 6},
        configurable={TOKEN_BUDGET_KEY: TokenBudget(limit=100, spent=90)},
    )
    prompt = llm.seen[0]
    assert len(prompt) == 1  # 贴在用户消息末尾, 没有另起一条
    block = _platform_block(prompt[0])
    assert _text(prompt[0]).startswith("go")
    assert _TOKEN_NOTICE_90 in block  # 措辞逐字不变, 只是挪进平台段
    assert block.index(_STEP_TAG) < block.index(_TOKEN_NOTICE_90)  # 步数预警在前
    assert block.count(PLATFORM_CONTEXT_OPEN) == 1


@dataclass
class _KeyProbe:
    """记下缓存 lookup(before)与 store(after)各自用来算 key 的提示词。"""

    seen: list[list[BaseMessage]]
    key: str
    name: str = "key_probe"
    anchor: str = "before_llm_call"
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        self.seen.append(list(ctx.payload.get(self.key) or []))
        await call_next(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(("step_count", "tag"), [(5, _STEP_TAG), (6, _WRAPUP_TAG)])
async def test_cache_lookup_and_store_see_the_same_final_prompt(step_count: int, tag: str) -> None:
    """响应缓存 lookup 的 key 与 store 的 key 都由最终提示词算(预警 / 收尾指令都已在内)。"""
    looked_up: list[list[BaseMessage]] = []
    stored: list[list[BaseMessage]] = []
    llm = _LLM([AIMessage(content="done")])
    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=_registry(saves=True),
        before_llm_chain=MiddlewareChain.from_middlewares(
            "before_llm_call", [_KeyProbe(looked_up, "messages")]
        ),
        after_llm_chain=MiddlewareChain.from_middlewares(
            "after_llm_call",
            [_KeyProbe(stored, "prompt_messages", anchor="after_llm_call")],
        ),
    )
    payload = {"messages": [HumanMessage(content="go")], "step_count": step_count, "max_steps": 6}
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        await compiled.ainvoke(payload, {"configurable": {"thread_id": str(uuid4())}})
    assert looked_up == stored == llm.seen
    assert tag in _text(looked_up[0][-1])


class _BlipError(Exception):
    pass


@dataclass
class _FlakyOnce:
    """第 ``fail_at`` 次调用抛一次瞬时错(HX-3 形态:整轮从检查点重跑这一步)。"""

    responses: list[AIMessage]
    fail_at: int
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        if self.calls == self.fail_at:
            raise AllProvidersExhaustedError(_BlipError("upstream 503"))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_step_warning_frame_survives_a_transient_retry_of_the_step() -> None:
    """B-226 ② —— 进入窗口的那一步模型调用瞬时失败、HX-3 从检查点重跑这一步:warning 帧仍
    只有一条(帧在调用成功之后才发;以前调用前就发了, 重跑再发一次)。"""
    llm = _FlakyOnce([_call("c0"), AIMessage(content="done")], fail_at=1)
    events, publish = _sink()
    graph = build_react_graph(llm_caller=llm, tool_registry=_registry(saves=True))
    cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4()), GUARD_SINK_KEY: publish}}
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        with pytest.raises(AllProvidersExhaustedError):
            await compiled.ainvoke(
                {"messages": [HumanMessage(content="go")], "step_count": 4, "max_steps": 6}, cfg
            )
        await compiled.ainvoke(None, cfg)  # run_agent 的 HX-3 重试:graph_input=None
    assert llm.calls == 3
    assert [e for e in events if e["kind"] == "warning"] == [
        {"kind": "warning", "guard": "max_steps", "detail": {"steps": 4, "max": 6}}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "discarded",
    [
        AIMessage(content="", response_metadata={"finish_reason": "length"}),  # B-158 截断
        AIMessage(content="", response_metadata={"finish_reason": "stop"}),  # B-191 空回复
    ],
    ids=["truncation-retry", "empty-reply-retry"],
)
async def test_step_warning_flag_rides_the_reply_retry_paths(discarded: AIMessage) -> None:
    """B-226 ② —— 进入窗口那一步的回答被丢弃重来(截断 / 空回复):重来的那一步不再发帧。"""
    llm = _LLM([discarded, AIMessage(content="done")])
    events, publish = _sink()
    state = await _run(
        llm,
        {"messages": [HumanMessage(content="go")], "step_count": 4, "max_steps": 6},
        configurable={GUARD_SINK_KEY: publish},
    )
    assert len(llm.seen) == 2
    assert state["messages"][-1].content == "done"
    assert [e["kind"] for e in events] == ["warning"]
