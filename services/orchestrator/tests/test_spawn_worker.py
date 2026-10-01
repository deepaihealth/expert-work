"""Unit tests for 1.3 dynamic Orchestrator-Worker — ``spawn_worker`` tool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from expert_work.runtime.cancellation import CancellationToken, RunCancelledError
from orchestrator.agent_factory import BuiltAgent
from orchestrator.errors import MaxStepsExceededError
from orchestrator.tools import ToolBlockedError, ToolContext
from orchestrator.tools.registry import ToolCatalogEntry
from orchestrator.tools.spawn_worker import (
    SPAWN_WORKER_TOOL_NAME,
    SpawnWorkerTool,
    WorkerAgentBuilder,
    WorkerSpawnBudget,
)


@dataclass
class _FakeGraph:
    result: dict[str, Any] | None = None
    raises: BaseException | None = None
    calls: list[tuple[Any, Any]] = field(default_factory=list)

    async def ainvoke(self, state: Any, config: Any) -> Any:
        self.calls.append((state, config))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del stream_mode
        result = await self.ainvoke(state, config)
        yield ("values", result)


@dataclass
class _RecordingWorkerBuilder:
    """Conforms to :class:`WorkerAgentBuilder`; records kwargs + returns a
    scripted :class:`BuiltAgent`."""

    built: BuiltAgent | None = None
    raises: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        role: str | None,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> BuiltAgent:
        self.calls.append(
            {"tenant_id": tenant_id, "role": role, "depth": depth, "oauth_user_id": oauth_user_id}
        )
        if self.raises is not None:
            raise self.raises
        assert self.built is not None
        return self.built


def _built(graph: _FakeGraph, *, system_prompt: str = "worker prompt") -> BuiltAgent:
    return BuiltAgent(graph=graph, system_prompt=system_prompt, max_steps=5)  # type: ignore[arg-type]


def _answer_graph(text: str) -> _FakeGraph:
    msgs = [HumanMessage(content="t"), AIMessage(content=text)]
    return _FakeGraph(result={"messages": msgs, "step_count": 2})


def _ctx(*, tenant_id: UUID | None = None, **kw: Any) -> ToolContext:
    return ToolContext(
        tenant_id=uuid4() if tenant_id is None else tenant_id,
        cancellation_token=CancellationToken(),
        **kw,
    )


def _tool(builder: WorkerAgentBuilder, *, child_depth: int = 1) -> SpawnWorkerTool:
    return SpawnWorkerTool(builder=builder, child_depth=child_depth)


# --- tool spec ---------------------------------------------------------------


def test_spec_name_and_params() -> None:
    tool = _tool(_RecordingWorkerBuilder())
    spec = tool.spec
    assert spec.name == SPAWN_WORKER_TOOL_NAME == "spawn_worker"
    assert spec.parameters["required"] == ["task"]
    assert "focus" in spec.parameters["properties"]
    assert spec.is_parallel_safe is True


def test_spec_description_carries_shape_criteria() -> None:
    """委派率增强(层 0)— the description carries the domain-free shape rubric,
    so the delegation judgment lives at the decision site. Pinned phrase by
    phrase so a future rewrite cannot silently drop a criterion."""
    desc = _tool(_RecordingWorkerBuilder()).spec.description
    # Worker profile: fresh context, concurrent, NOT cheap (a worker is a full run).
    assert "sees none of this conversation" in desc
    assert "run concurrently" in desc
    # 真栈对照(2026-08-28,run eac902ed)逮到的信息缺口:kimi 思考原文
    # 「spawn_worker 有工具吗?」——不确定 worker 能不能自己拉数,于是放弃
    # 委派取数型任务。工具继承必须写明,且钉住短语防止未来改丢。
    # B-122 —— worker 只拿到读侧工具(平台默认),短语随之改为如实描述。
    assert "read-side tools" in desc
    assert "MCP tools" in desc
    # 委派契约含工具指引(照 Anthropic cookbook / deepagents 的委派三要素)。
    assert "which tools to use" in desc
    # Proactive trigger — don't wait for the user to ask for delegation.
    assert "proactively" in desc
    # Positive shape criteria (should-delegate).
    assert "three or more similar, mutually independent sub-items" in desc
    assert "read in full" in desc
    assert "exploratory search" in desc
    # Negative criteria — external side effects / final call stay with the caller;
    # intermediate workspace files are fine (no blanket "writes" ban).
    assert "Do NOT use" in desc
    assert "external side effects" in desc
    assert "registering deliverables" in desc
    assert "final call on deliverables" in desc
    assert "work involving writes" not in desc
    assert "Workers may write intermediate files in the shared workspace" in desc
    # Self-contained task contract + verify-results clause.
    assert "self-contained" in desc
    assert "verify" in desc


# --- happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_builds_worker_and_returns_final_answer() -> None:
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("worker done")))
    tool = _tool(builder, child_depth=2)
    result = await tool.call({"task": "summarize X", "focus": "researcher"}, ctx=_ctx())
    assert result.content == "worker done"
    # focus → role; depth passed through to the builder.
    assert builder.calls[0]["role"] == "researcher"
    assert builder.calls[0]["depth"] == 2
    assert result.meta["dynamic"] is True
    assert result.meta["role"] == "researcher"
    # the task is seeded as the worker's HumanMessage.
    state, _cfg = builder.built.graph.calls[0]  # type: ignore[union-attr]
    assert isinstance(state["messages"][0], SystemMessage)
    assert state["messages"][1].content == "summarize X"


@pytest.mark.asyncio
async def test_focus_omitted_means_general_role() -> None:
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    result = await _tool(builder).call({"task": "do it"}, ctx=_ctx())
    assert builder.calls[0]["role"] is None
    assert result.meta["role"] is None


# --- guards ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tenant_raises_blocked() -> None:
    tool = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("x"))))
    with pytest.raises(ToolBlockedError):
        await tool.call({"task": "x"}, ctx=ToolContext(tenant_id=None))


@pytest.mark.asyncio
async def test_empty_task_raises_value_error() -> None:
    tool = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("x"))))
    with pytest.raises(ValueError, match="non-empty 'task'"):
        await tool.call({"task": "   "}, ctx=_ctx())


@pytest.mark.asyncio
async def test_expired_deadline_declines() -> None:
    tool = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("x"))))
    with pytest.raises(RunCancelledError):
        await tool.call({"task": "x"}, ctx=_ctx(deadline_at=0.0))


@pytest.mark.asyncio
async def test_max_steps_is_partial_result_not_error() -> None:
    builder = _RecordingWorkerBuilder(
        built=_built(_FakeGraph(raises=MaxStepsExceededError(step_count=5, max_steps=5)))
    )
    result = await _tool(builder).call({"task": "x"}, ctx=_ctx())
    assert "step limit" in result.content
    assert result.meta.get("subagent_max_steps") is True


# --- per-run budget ----------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_blocks_after_max_per_run() -> None:
    budget = WorkerSpawnBudget(max_per_run=2, max_concurrent=4)
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    tool = _tool(builder)
    ctx = _ctx(worker_spawn_budget=budget)
    r1 = await tool.call({"task": "a"}, ctx=ctx)
    r2 = await tool.call({"task": "b"}, ctx=ctx)
    r3 = await tool.call({"task": "c"}, ctx=ctx)
    assert r1.content == "ok"
    assert r2.content == "ok"
    # third spawn exceeds the per-run cap → soft refusal, builder not called.
    assert r3.meta.get("spawn_worker_blocked") is True
    assert len(builder.calls) == 2


def test_budget_try_reserve_counts() -> None:
    b = WorkerSpawnBudget(max_per_run=2, max_concurrent=1)
    assert b.try_reserve() is True
    assert b.try_reserve() is True
    assert b.try_reserve() is False


@pytest.mark.asyncio
async def test_budget_semaphore_bounds_concurrency() -> None:
    budget = WorkerSpawnBudget(max_per_run=10, max_concurrent=2)
    peak = 0
    live = 0

    async def _slow_ainvoke(state: Any, config: Any) -> Any:
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return {"messages": [AIMessage(content="ok")], "step_count": 1}

    @dataclass
    class _SlowGraph:
        async def ainvoke(self, state: Any, config: Any) -> Any:
            return await _slow_ainvoke(state, config)

        async def astream(
            self, state: Any, config: Any = None, *, stream_mode: Any = None
        ) -> AsyncIterator[Any]:
            del stream_mode
            result = await self.ainvoke(state, config)
            yield ("values", result)

    builder = _RecordingWorkerBuilder(built=_built(_SlowGraph()))  # type: ignore[arg-type]
    tool = _tool(builder)
    ctx = _ctx(worker_spawn_budget=budget)
    await asyncio.gather(*(tool.call({"task": f"t{i}"}, ctx=ctx) for i in range(6)))
    assert peak <= 2


# --- protocol ----------------------------------------------------------------


def test_worker_agent_builder_protocol_accepts_conforming() -> None:
    assert isinstance(_RecordingWorkerBuilder(), WorkerAgentBuilder)


# --- registration gating (build_tool_registry) -------------------------------

from expert_work.protocol import AgentSpec  # noqa: E402
from orchestrator.tools import ToolEnv  # noqa: E402
from orchestrator.tools.assembly import build_tool_registry  # noqa: E402
from orchestrator.tools.subagent import MAX_SUBAGENT_DEPTH  # noqa: E402

_PARENT = AgentSpec.model_validate(
    {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": "boss", "version": "1.0.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "deepseek", "name": "x"},
            "system_prompt": {"template": "hi"},
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": []},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
        },
    }
)


async def _fake_wbf(
    parent_spec: AgentSpec,
    *,
    tenant_id: UUID,
    role: str | None,
    depth: int,
    oauth_user_id: str | None = None,
) -> Any:
    return _built(_answer_graph("ok"))


async def _registry(*, worker_build_fn: Any, enabled: bool = True, depth: int = 0) -> Any:
    parent = _PARENT
    if not enabled:
        data = parent.model_dump(by_alias=True)
        data["spec"]["dynamic_workers"] = {"enabled": False}
        parent = AgentSpec.model_validate(data)
    return await build_tool_registry(
        [],
        tool_env=ToolEnv(worker_build_fn=worker_build_fn),
        parent_spec=parent,
        dynamic_workers=parent.spec.dynamic_workers,
        subagent_depth=depth,
    )


@pytest.mark.asyncio
async def test_registers_spawn_worker_when_wired_and_enabled() -> None:
    reg = await _registry(worker_build_fn=_fake_wbf)
    assert reg.get("spawn_worker") is not None


@pytest.mark.asyncio
async def test_no_spawn_worker_when_builder_unwired() -> None:
    reg = await _registry(worker_build_fn=None)
    assert reg.get("spawn_worker") is None


@pytest.mark.asyncio
async def test_no_spawn_worker_when_opted_out() -> None:
    reg = await _registry(worker_build_fn=_fake_wbf, enabled=False)
    assert reg.get("spawn_worker") is None


@pytest.mark.asyncio
async def test_no_spawn_worker_at_depth_cap() -> None:
    reg = await _registry(worker_build_fn=_fake_wbf, depth=MAX_SUBAGENT_DEPTH)
    assert reg.get("spawn_worker") is None


def test_spec_description_carries_delegation_contract_four_elements() -> None:
    """B-37 —— Anthropic 多智能体研究系统的委派四要素:目标 / 输出格式 /
    工具与数据源 / **任务边界**。原文:"Without detailed task descriptions,
    agents duplicate work, leave gaps, or fail to find necessary information."
    MAST 论文的干预实验:只改进角色规格说明,成功率 +9.4%。逐条钉住。"""
    desc = _tool(_RecordingWorkerBuilder()).spec.description
    assert "objective" in desc
    assert "output format" in desc
    assert "which tools to use" in desc
    assert "boundaries" in desc


def test_spec_description_prefers_path_reference_over_copying() -> None:
    """B-37 —— worker 与调用方共享工作区。约定文件应**给路径让 worker 自己读**,
    而不是把文件内容抄进 task:抄写会漏、会用到旧版本(Cognition 的 lossy
    prompt-copying 论证),而 worker 读到的永远是最新版。"""
    desc = _tool(_RecordingWorkerBuilder()).spec.description
    assert "workspace" in desc
    assert "by path" in desc


def test_spec_description_asks_worker_to_offload_bulk_output() -> None:
    """B-37 —— 大块产出落盘回引用,而非穿过对话历史(Anthropic:"pass
    lightweight references back to the coordinator... reduces token overhead
    from copying large outputs through conversation history")。"""
    desc = _tool(_RecordingWorkerBuilder()).spec.description.lower()
    assert "write the result to a file" in desc


# --- 本轮附件结构性下传 ---------------------------------------------------------
#
# 真栈实证(thread 4f236215,2026-08-29):主 Agent 派 worker 时任务文本只写了
# 「2. 分析资料内容:识别其中的流程步骤…」,一个字没提是哪份文件。worker 不继承
# 对话,看不到 ``[file attached: …]``,只能在共享工作区里按名字挑 —— 挑中了上一轮
# 的历史文档,做完一整轮才被主 Agent 发现方向错了,重派一次多花约 8 分钟。
#
# 委派契约要求主 Agent 把标识符写进 task,但那是**指令不是保证**。这几条钉的是
# 「即便主 Agent 忘了写,子代照样知道本轮附件是哪份」。


@pytest.mark.asyncio
async def test_worker_seed_carries_this_turns_attachments() -> None:
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_built(graph))
    ctx = _ctx(turn_documents=("uploads/糖尿病逆转_SOP.docx",))

    await _tool(builder).call({"task": "分析资料内容"}, ctx=ctx)

    seed = graph.calls[0][0]["messages"][1].content
    assert "uploads/糖尿病逆转_SOP.docx" in seed
    # 主 Agent 的原话仍在,且排在附件块之前 —— 附件是补充上下文,不是替换指令。
    assert seed.index("分析资料内容") < seed.index("uploads/糖尿病逆转_SOP.docx")


@pytest.mark.asyncio
async def test_worker_seed_unchanged_when_the_turn_has_no_attachment() -> None:
    """没有附件时 seed 必须与 task 逐字节相同 —— 不给子代凭空多一段噪声。"""
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_built(graph))

    await _tool(builder).call({"task": "分析资料内容"}, ctx=_ctx())

    assert graph.calls[0][0]["messages"][1].content == "分析资料内容"


@pytest.mark.asyncio
async def test_worker_seed_lists_every_attachment_of_the_turn() -> None:
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_built(graph))
    ctx = _ctx(turn_documents=("uploads/a.docx", "uploads/b.pptx"))

    await _tool(builder).call({"task": "对比这两份材料"}, ctx=ctx)

    seed = graph.calls[0][0]["messages"][1].content
    assert "uploads/a.docx" in seed
    assert "uploads/b.pptx" in seed


@pytest.mark.asyncio
async def test_attachments_reach_a_grandchild_worker() -> None:
    """worker 再派孙 worker 时,孙代同样不继承对话。断在深一层等于退回原来的猜,
    所以 ``_child_config`` 必须继续下传。"""
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_built(graph))
    ctx = _ctx(turn_documents=("uploads/糖尿病逆转_SOP.docx",))

    await _tool(builder).call({"task": "分析"}, ctx=ctx)

    child_state = graph.calls[0][0]
    assert child_state["turn_documents"] == ["uploads/糖尿病逆转_SOP.docx"]


# --- 图片:按**子代自己**的能力分流 -------------------------------------------
#
# 子代能不能看图,取决于它自己的模型 —— 不是父的。``dynamic_workers.model`` 可以
# 把 worker 换成另一个模型,于是父子能力可以相反,而且换得越强越容易出事:换成
# 多模态模型后,继承来的 ``vision:`` 块会被 agent_factory 忽略(没有 ask_image),
# 而原生看图那条路要求图片以 content block 贴在消息上 —— 子代种子若只有纯文本,
# 它就一张图都看不了,且不报错。


def _catalog(*names: str) -> tuple[ToolCatalogEntry, ...]:
    return tuple(
        ToolCatalogEntry(
            name=n,
            description="",
            parameters={},
            source="builtin",
            from_skill=None,
            deferred=False,
        )
        for n in names
    )


def _vision_built(graph: _FakeGraph) -> BuiltAgent:
    """原生多模态子代:supports_vision,且**没有** ask_image(块被忽略了)。"""
    return BuiltAgent(  # type: ignore[arg-type]
        graph=graph, system_prompt="p", max_steps=5, supports_vision=True, tool_catalog=_catalog()
    )


def _ask_image_built(graph: _FakeGraph) -> BuiltAgent:
    """Path B 子代:不支持视觉,但继承了 vision: 块所以有 ask_image。"""
    return BuiltAgent(  # type: ignore[arg-type]
        graph=graph,
        system_prompt="p",
        max_steps=5,
        supports_vision=False,
        tool_catalog=_catalog("ask_image"),
    )


@pytest.mark.asyncio
async def test_vision_capable_child_gets_the_image_as_a_content_block() -> None:
    """原生多模态子代拿到的是 ``image_ref`` block —— provider 调用前会把它解析成
    真图。这是这种子代**唯一**的通路:它没有 ask_image,纯文本里的 URI 对它就是
    一串它使不上的字符。"""
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_vision_built(graph))
    ctx = _ctx(turn_image_refs=("expert_work://image/x",))

    await _tool(builder).call({"task": "看看这张图"}, ctx=ctx)

    content = graph.calls[0][0]["messages"][1].content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看看这张图"}
    assert content[1] == {"type": "image_ref", "ref": "expert_work://image/x"}


@pytest.mark.asyncio
async def test_ask_image_child_gets_the_reference_as_text() -> None:
    """Path B 子代拿 URI 文本,自己去调 ask_image —— 图片字节不进它的上下文。"""
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_ask_image_built(graph))
    ctx = _ctx(turn_image_refs=("expert_work://image/x",))

    await _tool(builder).call({"task": "看看这张图"}, ctx=ctx)

    content = graph.calls[0][0]["messages"][1].content
    assert isinstance(content, str)
    assert "expert_work://image/x" in content
    assert "ask_image" in content


@pytest.mark.asyncio
async def test_child_that_can_neither_see_nor_ask_is_told_nothing_about_images() -> None:
    """既不支持视觉、又没有 ask_image 的子代,不提图片。

    它对这些图既看不了也问不了;把 URI 摆在它面前只会引它编一个答案出来。"""
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_built(graph))  # 无 vision、空 catalog
    ctx = _ctx(turn_image_refs=("expert_work://image/x",))

    await _tool(builder).call({"task": "看看这张图"}, ctx=ctx)

    assert graph.calls[0][0]["messages"][1].content == "看看这张图"


@pytest.mark.asyncio
async def test_child_capability_is_read_from_the_child_not_the_parent() -> None:
    """判据必须取自子代的 ``BuiltAgent``。父是不是多模态、父有没有 ask_image,
    在这里都无从得知也不该参与 —— worker 的模型可以被 manifest 覆盖成另一个。"""
    graph = _answer_graph("done")
    # 子代是 Path B(非视觉 + ask_image);若实现误取父的能力,这里拿不到文本 URI。
    builder = _RecordingWorkerBuilder(built=_ask_image_built(graph))
    ctx = _ctx(turn_image_refs=("expert_work://image/x",))

    await _tool(builder).call({"task": "t"}, ctx=ctx)

    assert isinstance(graph.calls[0][0]["messages"][1].content, str)


@pytest.mark.asyncio
async def test_documents_and_images_ride_together() -> None:
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_vision_built(graph))
    ctx = _ctx(
        turn_documents=("uploads/a.docx",),
        turn_image_refs=("expert_work://image/x",),
    )

    await _tool(builder).call({"task": "对照文档看图"}, ctx=ctx)

    content = graph.calls[0][0]["messages"][1].content
    assert isinstance(content, list)
    # 文档进文本块(多模态子代照样用 read_document 读它),图片进 image_ref block。
    assert "uploads/a.docx" in content[0]["text"]
    assert content[1]["ref"] == "expert_work://image/x"


@pytest.mark.asyncio
async def test_image_refs_reach_a_grandchild_worker() -> None:
    graph = _answer_graph("done")
    builder = _RecordingWorkerBuilder(built=_ask_image_built(graph))
    ctx = _ctx(turn_image_refs=("expert_work://image/x",))

    await _tool(builder).call({"task": "t"}, ctx=ctx)

    assert graph.calls[0][0]["turn_image_refs"] == ["expert_work://image/x"]


# --- 嵌套 worker 也计入本 run 的预算(B1)+ 先占 per-run 并发再占全局闸(B2) -------


@dataclass
class _NestedSpawnGraph:
    """一个 depth-1 worker 的图:运行时用**自己拿到的 config** 还原 ToolContext
    (与真 ``tools_node`` 同一条路),再以 depth 2 派 ``nested`` 个孙 worker。

    孙 worker 的结果收在 ``nested_results`` 里供断言;图本身返回一句话。"""

    nested: int
    nested_results: list[Any] = field(default_factory=list)

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del state, stream_mode
        from orchestrator.graph_builder.builder import _build_tool_context

        inner_ctx = _build_tool_context(config)
        inner = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("leaf"))), child_depth=2)
        for i in range(self.nested):
            self.nested_results.append(await inner.call({"task": f"leaf {i}"}, ctx=inner_ctx))
        yield ("values", {"messages": [AIMessage(content="parent done")], "step_count": 1})


@pytest.mark.asyncio
async def test_nested_workers_spend_the_same_per_run_budget() -> None:
    """depth-2 的 spawn 以前看到的是 ``worker_spawn_budget=None`` —— 不限个数、
    不限并发。现在与顶层共用同一个预算对象:2 个名额,顶层用掉 1 个,
    孙代第 1 个拿到最后一个,第 2 个被拒。"""
    budget = WorkerSpawnBudget(max_per_run=2, max_concurrent=4)
    graph = _NestedSpawnGraph(nested=2)
    tool = _tool(_RecordingWorkerBuilder(built=_built(graph)))  # type: ignore[arg-type]

    result = await tool.call({"task": "fan out"}, ctx=_ctx(worker_spawn_budget=budget))

    assert result.content == "parent done"
    first, second = graph.nested_results
    assert first.content == "leaf"
    assert second.meta.get("spawn_worker_blocked") is True
    assert "worker budget (2)" in second.content


@pytest.mark.asyncio
async def test_nested_worker_does_not_take_a_concurrency_slot() -> None:
    """max_concurrent=1:顶层 worker 占着唯一的并发位等它的孙 worker。若孙代也要
    并发位,父等子、子等父 —— 死锁。规则:depth>=2 只扣个数、不占并发位。"""
    budget = WorkerSpawnBudget(max_per_run=5, max_concurrent=1)
    graph = _NestedSpawnGraph(nested=2)
    tool = _tool(_RecordingWorkerBuilder(built=_built(graph)))  # type: ignore[arg-type]

    result = await asyncio.wait_for(
        tool.call({"task": "fan out"}, ctx=_ctx(worker_spawn_budget=budget)), timeout=2.0
    )

    assert result.content == "parent done"
    assert [r.content for r in graph.nested_results] == ["leaf", "leaf"]


@dataclass
class _BlockingGraph:
    """跑起来就停在 ``release`` 上,直到测试放行。"""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del state, config, stream_mode
        self.started.set()
        await self.release.wait()
        yield ("values", {"messages": [AIMessage(content="ok")], "step_count": 1})


@pytest.mark.asyncio
async def test_worker_queued_on_per_run_concurrency_holds_no_gate_slot() -> None:
    """排在本 run 并发位后面的 worker 不许占着全进程的委派闸 —— 那等于拿别的
    run 的名额来排自己的队。全局闸容量 2、本 run 并发 1:第二个 worker 等并发位
    时,闸里只有第一个。"""
    from orchestrator.tools._budget import DelegationGate

    async def _cap() -> int:
        return 2

    gate = DelegationGate(_cap, timeout_s=5.0)
    budget = WorkerSpawnBudget(max_per_run=5, max_concurrent=1)
    graph = _BlockingGraph()
    builder = _RecordingWorkerBuilder(built=_built(graph))  # type: ignore[arg-type]
    tool = _tool(builder)
    ctx = _ctx(worker_spawn_budget=budget, delegation_gate=gate)

    first = asyncio.create_task(tool.call({"task": "a"}, ctx=ctx))
    await asyncio.wait_for(graph.started.wait(), timeout=2.0)
    second = asyncio.create_task(tool.call({"task": "b"}, ctx=ctx))
    for _ in range(5):
        await asyncio.sleep(0)

    assert gate.active == 1  # 只有在跑的那个;排队的没占闸
    assert len(builder.calls) == 1  # 排队的也还没付构建的钱

    graph.release.set()
    r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)
    assert (r1.content, r2.content) == ("ok", "ok")
    assert gate.active == 0


@pytest.mark.asyncio
async def test_gate_refusal_gives_back_the_concurrency_slot() -> None:
    """闸拒了 → 已经拿到的并发位必须还回去,否则本 run 后面的 worker 永远排不上。"""

    @dataclass
    class _RefusingGate:
        async def acquire(self, *, timeout_s: float | None = None) -> bool:
            return False

        async def release(self) -> None:
            raise AssertionError("refused acquire must not be released")

    budget = WorkerSpawnBudget(max_per_run=5, max_concurrent=1)
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    tool = _tool(builder)

    refused = await tool.call(
        {"task": "a"}, ctx=_ctx(worker_spawn_budget=budget, delegation_gate=_RefusingGate())
    )
    assert refused.meta.get("delegation_gated") is True

    ok = await asyncio.wait_for(
        tool.call({"task": "b"}, ctx=_ctx(worker_spawn_budget=budget)), timeout=2.0
    )
    assert ok.content == "ok"


@pytest.mark.asyncio
async def test_builder_failure_gives_back_the_concurrency_slot() -> None:
    budget = WorkerSpawnBudget(max_per_run=5, max_concurrent=1)
    failing = _tool(_RecordingWorkerBuilder(raises=RuntimeError("build boom")))
    with pytest.raises(RuntimeError, match="build boom"):
        await failing.call({"task": "a"}, ctx=_ctx(worker_spawn_budget=budget))

    ok_tool = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("ok"))))
    ok = await asyncio.wait_for(
        ok_tool.call({"task": "b"}, ctx=_ctx(worker_spawn_budget=budget)), timeout=2.0
    )
    assert ok.content == "ok"
