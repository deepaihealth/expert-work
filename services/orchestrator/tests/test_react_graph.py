"""Unit tests for :func:`build_react_graph` (Stream E.6).

Uses an in-memory checkpointer + a scripted ``LLMCaller`` that returns
a predetermined sequence of ``AIMessage`` values. No real LLM call,
no middleware chain wired — this PR is about loop / dispatch /
error-wrap mechanics; middleware integration follows in E.11.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    AgentState,
    GraphRunner,
    LLMCaller,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    """LLMCaller stub: returns ``responses[call_index]`` on each invocation."""

    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        idx = self.calls
        self.calls += 1
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out of responses at call {idx}")
        return self.responses[idx]


@dataclass
class _ScriptedTool:
    """Tool stub: returns ``result``, or raises ``exc`` if set."""

    name: str
    result: str = ""
    exc: Exception | None = None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"scripted {self.name}")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        if self.exc is not None:
            raise self.exc
        return ToolResult(content=self.result)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    """Build the ``tool_calls`` entry LangChain expects on AIMessage."""
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


async def _run_graph(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    *,
    max_steps: int = 5,
    thread_id: str = "test-thread",
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        result = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "step_count": 0,
                "max_steps": max_steps,
            },
            config=cfg,
        )
        return result


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_step_final_answer() -> None:
    """LLM returns text only on the first call → ReAct loop ends after one
    agent step."""
    llm = _ScriptedLLM(responses=[AIMessage(content="all done")])
    registry = ToolRegistry()
    state = await _run_graph(llm, registry)
    assert llm.calls == 1
    assert state["step_count"] == 1
    assert state["messages"][-1].content == "all done"


@pytest.mark.asyncio
async def test_three_step_loop_tool_tool_final() -> None:
    """tool_call → tool_call → final answer (3 LLM calls, 2 tool dispatches)."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("search", {"q": "python"}, "tc-1")],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("search", {"q": "go"}, "tc-2")],
            ),
            AIMessage(content="found 2 results"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="search", result="result-body"))

    state = await _run_graph(llm, registry)
    assert llm.calls == 3
    assert state["step_count"] == 3

    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    assert all(m.content == "result-body" for m in tool_msgs)
    assert state["messages"][-1].content == "found 2 results"


# ---------------------------------------------------------------------------
# max_steps guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_steps_graceful_wrapup_when_llm_keeps_calling_tools() -> None:
    """LLM never finalises → at the budget the loop does ONE final tool-less
    wrap-up turn instead of raising, so the run ends cleanly without discarding
    work (hermes-agent #7915). The wrap-up response has its tool_calls stripped
    so the router goes to END."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("search", {"q": str(i)}, f"tc-{i}")],
            )
            for i in range(5)
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="search", result="r"))

    # No exception — the run finalises gracefully.
    state = await _run_graph(llm, registry, max_steps=3)
    # 3 normal turns (each dispatched a tool) + 1 forced tool-less wrap-up turn.
    assert llm.calls == 4
    assert state["step_count"] == 4
    # The wrap-up turn is terminal: tool_calls stripped → loop ended at END.
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    assert not last.tool_calls


@pytest.mark.asyncio
async def test_final_at_max_steps_runs_clean() -> None:
    """LLM returns final answer on step max_steps → no MaxStepsExceededError."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("search", {"q": "1"}, "tc-1")],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("search", {"q": "2"}, "tc-2")],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="search", result="r"))
    state = await _run_graph(llm, registry, max_steps=3)
    assert state["step_count"] == 3
    assert state["messages"][-1].content == "done"


# ---------------------------------------------------------------------------
# Tool error wrapper (Mini-ADR E-12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_exception_wrapped_into_toolmessage_error() -> None:
    """Tool raise → ToolMessage(content='[tool error] ...') injected; loop continues."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("search", {"q": "x"}, "tc-1")],
            ),
            AIMessage(content="ok, gave up on that tool"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="search", exc=RuntimeError("connection refused")))

    state = await _run_graph(llm, registry)
    assert llm.calls == 2
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content.startswith("[tool error] RuntimeError:")
    assert "connection refused" in tool_msgs[0].content
    assert tool_msgs[0].status == "error"
    assert state["messages"][-1].content == "ok, gave up on that tool"


@pytest.mark.asyncio
async def test_unknown_tool_wrapped_into_toolmessage_error() -> None:
    """LLM calls a tool that isn't registered → ToolMessage(error) + loop continues."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("ghost_tool", {}, "tc-1")],
            ),
            AIMessage(content="never mind"),
        ]
    )
    registry = ToolRegistry()

    state = await _run_graph(llm, registry)
    assert llm.calls == 2
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert "ToolNotFoundError" in tool_msgs[0].content
    assert "ghost_tool" in tool_msgs[0].content
    assert tool_msgs[0].status == "error"


@pytest.mark.asyncio
async def test_long_tool_error_truncated() -> None:
    """Multi-MB exception strings get capped before injection."""
    huge_msg = "x" * 5000
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("search", {"q": "x"}, "tc-1")],
            ),
            AIMessage(content="ok"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="search", exc=RuntimeError(huge_msg)))

    state = await _run_graph(llm, registry)
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert "[truncated]" in tool_msgs[0].content
    # Truncation cap is well under the original 5000 chars.
    assert len(tool_msgs[0].content) < 1000


# ---------------------------------------------------------------------------
# Parallel tool_calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_tool_calls_produce_separate_toolmessages() -> None:
    """LLM emits multiple tool_calls in one AIMessage → one ToolMessage each, in order."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("search", {"q": "a"}, "tc-1"),
                    _tool_call("search", {"q": "b"}, "tc-2"),
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="search", result="ok"))

    state = await _run_graph(llm, registry)
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["tc-1", "tc-2"]
    assert state["messages"][-1].content == "done"


# ---------------------------------------------------------------------------
# B-105 —— 输出被上限截断
# ---------------------------------------------------------------------------


def _truncated_count(usable: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "expert_work_llm_output_truncated_total",
        {"provider": "glm", "model": "glm-5.3", "usable": usable},
    )
    return value or 0.0


@dataclass
class _StreamingScriptedLLM(_ScriptedLLM):
    """``run_agent`` 接了 token 流,调用会多带 ``on_delta``;脚本回答不走流。"""

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        on_delta: Any = None,
    ) -> AIMessage:
        del on_delta
        return await super().__call__(messages=messages, tools=tools)


def _served_caps() -> Any:
    from expert_work.protocol import ModelSpec
    from orchestrator.llm.truncation import served_output_cap

    return served_output_cap(
        ModelSpec.model_validate(
            {
                "provider": "glm",
                "name": "glm-5.3",
                "max_tokens": 2048,
                "fallback": [{"provider": "qwen", "name": "qwen3.6-plus", "max_tokens": 8192}],
            }
        )
    )


_GLM_CAPS = _served_caps()


async def _run_through_sse(llm: LLMCaller, registry: ToolRegistry) -> tuple[list[Any], Any]:
    """真 graph 过 ``run_agent``:断言落在用户看得见的 error 帧与 run 行上。"""
    from uuid import uuid4

    from expert_work.runtime.runs import InMemoryRunStore, RunManager
    from expert_work.runtime.stream_bridge import InMemoryStreamBridge, is_end
    from orchestrator.sse import run_agent

    bridge = InMemoryStreamBridge()
    store = InMemoryRunStore()
    rm = RunManager(store=store)
    record = await rm.create(run_id=uuid4(), thread_id=uuid4(), tenant_id=uuid4())
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                output_cap_resolver=_GLM_CAPS,
            )
        )
        await run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=compiled,
            graph_input={
                "messages": [HumanMessage(content="start")],
                "step_count": 0,
                "max_steps": 5,
            },
            config={"configurable": {"thread_id": str(record.thread_id)}},
        )
    events: list[Any] = []
    async for entry in bridge.subscribe(record.run_id, heartbeat_interval=5.0):
        if is_end(entry):
            break
        events.append(entry)
    row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
    return events, row


@pytest.mark.asyncio
async def test_truncated_empty_reply_fails_the_run_visibly() -> None:
    # 思考模型截断的实测形态:额度全给了思考,正文为空。B-194:重试 3 次仍截断才报错。
    before = _truncated_count("false")
    truncated = AIMessage(content="", response_metadata={"finish_reason": "length"})
    llm = _StreamingScriptedLLM(responses=[truncated] * 4)

    events, row = await _run_through_sse(llm, ToolRegistry())

    errors = [e for e in events if e.event == "error"]
    assert len(errors) == 1
    assert errors[0].data["name"] == "OutputTruncatedError"
    assert "模型输出被截断" in errors[0].data["message"]
    assert "2048" in errors[0].data["message"]
    assert row is not None and row.error is not None and "模型输出被截断" in row.error
    assert _truncated_count("false") == before + 4, "四次截断各计一次"
    assert llm.calls == 4


@pytest.mark.asyncio
async def test_stall_after_reasoning_only_fails_the_run_visibly() -> None:
    # B-186:思考完停住、一个字的回答都没有。原来这一轮当成空回答收尾,run 记成功。
    # 真路由(盖章)→ 真图(主循环判失败)→ run_agent。
    import asyncio

    from orchestrator.llm.providers._streaming import LLMDelta, OpenAIStreamAssembler
    from orchestrator.llm.router import LLMRouter, ProviderHandle

    class _ThinksThenStalls:
        async def stream(self, *, messages: Any, tools: Any, output_schema: Any = None) -> Any:
            del messages, tools, output_schema
            yield LLMDelta(reasoning="让我想想")
            await asyncio.sleep(0.3)

        def new_stream_assembler(self) -> OpenAIStreamAssembler:
            return OpenAIStreamAssembler()

    router = LLMRouter(
        providers=[ProviderHandle(provider=_ThinksThenStalls(), key="deepseek:deepseek-flash")],
        first_token_timeout_s=0.5,
        idle_timeout_s=0.1,
    )

    events, row = await _run_through_sse(router, ToolRegistry())

    errors = [e for e in events if e.event == "error"]
    assert len(errors) == 1
    assert errors[0].data["name"] == "StalledWithoutAnswerError"
    assert "还没给出回答就中断了" in errors[0].data["message"]
    assert "deepseek" not in errors[0].data["message"], "不暴露模型与凭据序号"
    assert row is not None and row.status.value == "error"
    assert row.error is not None and "token 间隔超时" in row.error


@pytest.mark.asyncio
async def test_truncated_tool_call_fails_before_dispatch() -> None:
    # 截断时最后一个工具调用的参数必然被截:不能把残缺参数派给工具(重试那次也一样)。
    truncated = AIMessage(
        content="好的,我来写文件",
        tool_calls=[_tool_call("write_file", {}, "tc-1")],
        response_metadata={"stop_reason": "max_tokens"},
    )
    llm = _StreamingScriptedLLM(responses=[truncated] * 4)
    tool = _CountingTool(name="write_file")
    registry = ToolRegistry()
    registry.register(tool)

    events, _ = await _run_through_sse(llm, registry)

    assert [e.data["name"] for e in events if e.event == "error"] == ["OutputTruncatedError"]
    assert tool.calls == 0
    assert llm.calls == 4


@pytest.mark.asyncio
async def test_truncated_but_usable_text_is_delivered_and_counted() -> None:
    before = _truncated_count("true")
    llm = _StreamingScriptedLLM(
        responses=[
            AIMessage(content="一段完整度够用的回答", response_metadata={"finish_reason": "length"})
        ]
    )

    events, _ = await _run_through_sse(llm, ToolRegistry())

    assert [e for e in events if e.event == "error"] == []
    assert _truncated_count("true") == before + 1


@pytest.mark.asyncio
async def test_truncation_without_wiring_labels_is_still_judged() -> None:
    # 不接 output_cap / 模型名(单测 / 子图)也判;报错文案退到「厂商默认值」。
    from orchestrator.llm.truncation import OutputTruncatedError

    truncated = AIMessage(content="", response_metadata={"finish_reason": "length"})
    llm = _ScriptedLLM(responses=[truncated] * 4)
    with pytest.raises(OutputTruncatedError, match="厂商默认值"):
        await _run_graph(llm, ToolRegistry())


@dataclass
class _CountingTool:
    name: str
    calls: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"counting {self.name}")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.calls += 1
        return ToolResult(content="ok")


def _count(provider: str, model: str, usable: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "expert_work_llm_output_truncated_total",
        {"provider": provider, "model": model, "usable": usable},
    )
    return value or 0.0


@pytest.mark.asyncio
async def test_fallback_served_truncation_reports_the_fallback_cap_and_label() -> None:
    # 备用接管后截断:文案里的上限与计数器标签都按实际应答的模型(路由盖的章)。
    from orchestrator.llm.truncation import OutputTruncatedError
    from orchestrator.usage_metering import SERVED_BY_KEY

    before = _count("qwen", "qwen3.6-plus", "false")
    truncated = AIMessage(
        content="",
        response_metadata={"finish_reason": "length", SERVED_BY_KEY: "qwen:qwen3.6-plus#1"},
    )
    llm = _ScriptedLLM(responses=[truncated] * 4)
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm, tool_registry=ToolRegistry(), output_cap_resolver=_GLM_CAPS
            )
        )
        with pytest.raises(OutputTruncatedError) as info:
            await compiled.ainvoke(
                {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 5},
                config={"configurable": {"thread_id": "t-fallback"}},
            )

    assert info.value.cap == 8192 and "8192" in str(info.value)
    assert _count("qwen", "qwen3.6-plus", "false") == before + 4


def test_served_output_cap_falls_back_to_the_primary_when_unstamped() -> None:
    assert _GLM_CAPS(AIMessage(content="")) == ("glm", "glm-5.3", 2048)


@dataclass
class _SpyCache:
    puts: list[AIMessage] = field(default_factory=list)

    def make_key(self, **kwargs: Any) -> str:
        del kwargs
        return "k"

    async def put(self, key: str, response: AIMessage, ttl_s: int | None = None) -> None:
        del key, ttl_s
        self.puts.append(response)


async def _run_with_usage(response: AIMessage) -> tuple[Any, _SpyCache, BaseException | None]:
    from uuid import uuid4

    from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore
    from expert_work.runtime.middleware import MiddlewareChain, TokenUsageMiddleware
    from expert_work.runtime.middleware.llm_cache import LLMCacheStoreMiddleware

    store = InMemoryTokenUsageStore()
    cache = _SpyCache()
    after = MiddlewareChain.from_middlewares(
        "after_llm_call",
        [
            TokenUsageMiddleware(
                store=store,
                agent_name="a",
                agent_version="1",
                model="glm-5.3",
                provider="glm",
                usage_kind="conversation",
            ),
            LLMCacheStoreMiddleware(cache=cache, model="glm-5.3", temperature=0.0, max_tokens=2048),  # type: ignore[arg-type]
        ],
    )
    tenant = uuid4()
    raised: BaseException | None = None
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=_ScriptedLLM(responses=[response] * 4),
                tool_registry=ToolRegistry(),
                after_llm_chain=after,
                output_cap_resolver=_GLM_CAPS,
            )
        )
        try:
            await compiled.ainvoke(
                {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 5},
                config={"configurable": {"thread_id": str(uuid4()), "tenant_id": str(tenant)}},
            )
        except Exception as exc:
            raised = exc
    rows = await store.list_for_tenant(tenant_id=tenant)
    return rows, cache, raised


_USAGE = {"input_tokens": 100, "output_tokens": 2048, "total_tokens": 2148}


@pytest.mark.asyncio
async def test_unusable_truncation_is_metered_but_not_cached() -> None:
    # 截断那次调用照样计费(常常整个上限都花在思考上):落一行用量,但不进响应缓存。
    from orchestrator.llm.truncation import OutputTruncatedError

    rows, cache, raised = await _run_with_usage(
        AIMessage(content="", usage_metadata=_USAGE, response_metadata={"finish_reason": "length"})
    )

    assert isinstance(raised, OutputTruncatedError)
    assert [(r.model, r.input_tokens, r.output_tokens, r.usage_kind) for r in rows] == [
        ("glm-5.3", 100, 2048, "conversation")
    ] * 4, "B-158 / B-194:原调用与 3 次重试各计一行"
    assert cache.puts == []


@pytest.mark.asyncio
async def test_stall_without_answer_is_metered_but_not_cached() -> None:
    # B-186:停住那次调用照样计费(Anthropic 的半截带着输入用量),但空的半截不进响应缓存。
    from orchestrator.llm.stall import STALLED_AFTER_S, StalledWithoutAnswerError

    rows, cache, raised = await _run_with_usage(
        AIMessage(content="", usage_metadata=_USAGE, response_metadata={STALLED_AFTER_S: 45.0})
    )

    assert isinstance(raised, StalledWithoutAnswerError)
    assert [(r.input_tokens, r.output_tokens) for r in rows] == [(100, 2048)]
    assert cache.puts == []


@pytest.mark.asyncio
async def test_stall_after_answer_text_is_delivered_but_not_cached() -> None:
    # 停住前已经有回答文字:照旧交付、记账,但半句回答不进响应缓存。
    from orchestrator.llm.stall import STALLED_AFTER_S

    rows, cache, raised = await _run_with_usage(
        AIMessage(
            content="半句回答", usage_metadata=_USAGE, response_metadata={STALLED_AFTER_S: 45.0}
        )
    )

    assert raised is None
    assert len(rows) == 1
    assert cache.puts == []


@pytest.mark.asyncio
async def test_usable_truncation_still_goes_through_the_normal_after_chain() -> None:
    # 对照:同一套中间件在可用回答上会落缓存 —— 证明上一条的「没落缓存」咬得住。
    rows, cache, raised = await _run_with_usage(
        AIMessage(
            content="够用的回答",
            usage_metadata=_USAGE,
            response_metadata={"finish_reason": "length"},
        )
    )

    assert raised is None
    assert len(rows) == 1
    assert len(cache.puts) == 1


@pytest.mark.asyncio
async def test_budget_exhausted_wrap_up_with_junk_tool_calls_is_delivered() -> None:
    # 收尾轮(没绑工具)模型回的 tool_calls 本来就会被剥掉;剥完剩可用正文就照常交付。
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="这是收尾总结",
                tool_calls=[_tool_call("write_file", {}, "tc-1")],
                response_metadata={"finish_reason": "length"},
            )
        ]
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry())
        )
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 1, "max_steps": 1},
            config={"configurable": {"thread_id": "t-wrapup"}},
        )

    assert state["messages"][-1].content == "这是收尾总结"
    assert state["messages"][-1].tool_calls == []


# ---------------------------------------------------------------------------
# B-158 —— 截断不可用:丢弃、加拆小提示、重试一次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_tool_call_is_discarded_and_retried_once() -> None:
    # B-140 实测形态:想一口气把整份文件连思考写完,撞满上限。被截的回答不进历史、
    # 里面的工具调用不执行;模型看到隐藏提示后拆小重做,run 照常完成。
    from orchestrator.graph_builder.builder import TRUNCATION_RETRY_KEY

    before = _truncated_count("false")
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("write_file", {}, "tc-cut")],
                response_metadata={"finish_reason": "length"},
            ),
            AIMessage(content="分步写完了"),
        ]
    )
    tool = _CountingTool(name="write_file")
    registry = ToolRegistry()
    registry.register(tool)
    seen: list[Sequence[BaseMessage]] = []
    inner = llm.__call__

    async def spy(*, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]) -> AIMessage:
        seen.append(list(messages))
        return await inner(messages=messages, tools=tools)

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=spy, tool_registry=registry, output_cap_resolver=_GLM_CAPS)
        )
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 5},
            config={"configurable": {"thread_id": "t-b158"}},
        )

    assert tool.calls == 0, "被截的工具调用不执行"
    assert state["messages"][-1].content == "分步写完了"
    assert not any(getattr(m, "tool_calls", None) for m in state["messages"]), "被截的回答不进历史"
    notices = [m for m in state["messages"] if m.additional_kwargs.get(TRUNCATION_RETRY_KEY)]
    assert len(notices) == 1
    assert notices[0].additional_kwargs.get("expert_work_hide_from_ui") is True
    assert "2048" in str(notices[0].content)
    assert seen[1][-1] is not None and seen[1][-1].additional_kwargs.get(TRUNCATION_RETRY_KEY)
    assert _truncated_count("false") == before + 1


# ---------------------------------------------------------------------------
# B-191 —— 正常结束却既没有回答也没有工具调用:丢弃、加隐藏提示、重试一次
# ---------------------------------------------------------------------------

_STOP = {"finish_reason": "stop"}


def _empty_stop() -> AIMessage:
    # 每次新建:同一个实例落进历史会被分配 id,再次返回就成了「替换」而不是「追加」。
    return AIMessage(content="", response_metadata=_STOP)


def _cut() -> AIMessage:
    return AIMessage(content="", response_metadata={"finish_reason": "length"})


def _empty_count(outcome: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "expert_work_llm_empty_reply_total",
        {"provider": "glm", "model": "glm-5.3", "outcome": outcome},
    )
    return value or 0.0


def _empty_notices(state: AgentState) -> list[BaseMessage]:
    from orchestrator.graph_builder.builder import EMPTY_REPLY_RETRY_KEY

    return [m for m in state["messages"] if m.additional_kwargs.get(EMPTY_REPLY_RETRY_KEY)]


async def _invoke_scripted(
    llm: LLMCaller,
    *,
    thread_id: str,
    registry: ToolRegistry | None = None,
    step_count: int = 0,
    max_steps: int = 5,
    **graph_kwargs: Any,
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry or ToolRegistry(),
                output_cap_resolver=_GLM_CAPS,
                **graph_kwargs,
            )
        )
        return await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "step_count": step_count,
                "max_steps": max_steps,
            },
            config={"configurable": {"thread_id": thread_id}},
        )


@pytest.mark.asyncio
async def test_empty_normal_stop_is_discarded_and_retried_once() -> None:
    # 实测形态:工具调用写进了思考,正文与 tool_calls 都空、结束原因 stop。原来记成功。
    from orchestrator.graph_builder.builder import EMPTY_REPLY_RETRY_KEY

    before = _empty_count("retried")
    llm = _ScriptedLLM(
        responses=[_empty_stop(), AIMessage(content="这次答了", response_metadata=_STOP)]
    )
    seen: list[Sequence[BaseMessage]] = []
    inner = llm.__call__

    async def spy(*, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]) -> AIMessage:
        seen.append(list(messages))
        return await inner(messages=messages, tools=tools)

    state = await _invoke_scripted(spy, thread_id="t-b191")

    assert llm.calls == 2
    assert state["messages"][-1].content == "这次答了"
    assert [m.content for m in state["messages"] if isinstance(m, AIMessage)] == ["这次答了"], (
        "空回复不进历史"
    )
    notices = _empty_notices(state)
    assert len(notices) == 1
    assert notices[0].additional_kwargs.get("expert_work_hide_from_ui") is True
    assert "no answer and no tool call" in str(notices[0].content)
    assert "base64" in str(notices[0].content)
    assert seen[1][-1].additional_kwargs.get(EMPTY_REPLY_RETRY_KEY)
    assert state.get("exit_reason") == "text_response"
    assert _empty_count("retried") == before + 1


@pytest.mark.asyncio
async def test_fourth_empty_reply_fails_the_run_visibly() -> None:
    # B-194:重试 3 次仍空着才报错。
    before = _empty_count("failed")
    before_retried = _empty_count("retried")
    llm = _StreamingScriptedLLM(responses=[_empty_stop() for _ in range(4)])

    events, row = await _run_through_sse(llm, ToolRegistry())

    errors = [e for e in events if e.event == "error"]
    assert len(errors) == 1
    assert errors[0].data["name"] == "EmptyReplyError"
    assert "既没有给出回答" in errors[0].data["message"]
    assert "已自动重试过 3 次" in errors[0].data["message"]
    assert row is not None and row.status.value == "error"
    assert row.error is not None and "没有调用工具" in row.error
    assert llm.calls == 4
    assert _empty_count("retried") == before_retried + 3
    assert _empty_count("failed") == before + 1


@pytest.mark.asyncio
async def test_text_and_tool_call_replies_take_no_retry() -> None:
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="search", result="ok"))
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="", tool_calls=[_tool_call("search", {}, "tc-1")], response_metadata=_STOP
            ),
            AIMessage(content="done", response_metadata=_STOP),
        ]
    )

    state = await _invoke_scripted(llm, thread_id="t-b191-normal", registry=registry)

    assert llm.calls == 2
    assert state["messages"][-1].content == "done"
    assert _empty_notices(state) == []


@dataclass
class _CacheHit:
    """before-chain stub: every lookup hits with ``response``."""

    response: AIMessage
    name: str = "cache_hit_stub"
    anchor: str = "before_llm_call"
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()

    async def __call__(self, ctx: Any, call_next: Any) -> None:
        ctx.payload["llm_cache_hit"] = self.response
        await call_next(ctx)


@pytest.mark.asyncio
async def test_cache_hit_is_not_judged_again() -> None:
    # 缓存命中的旧回答不再判(与 B-105 / B-186 同一口径)。
    from expert_work.runtime.middleware import MiddlewareChain

    llm = _ScriptedLLM(responses=[])
    state = await _invoke_scripted(
        llm,
        thread_id="t-b191-cache",
        before_llm_chain=MiddlewareChain.from_middlewares(
            "before_llm_call",
            [_CacheHit(response=_empty_stop())],
        ),
    )

    assert llm.calls == 0
    assert _empty_notices(state) == []
    assert state["messages"][-1].content == ""


@pytest.mark.asyncio
async def test_empty_reply_on_the_budget_wrap_up_turn_is_not_retried() -> None:
    # 收尾轮没绑工具、run 按预算出口结束(不是「空着的成功」):不重试、不报错,照旧。
    llm = _ScriptedLLM(responses=[_empty_stop()])

    state = await _invoke_scripted(llm, thread_id="t-b191-wrapup", step_count=1, max_steps=1)

    assert llm.calls == 1
    assert _empty_notices(state) == []
    assert state.get("exit_reason") == "max_steps"


@pytest.mark.asyncio
async def test_discarded_empty_reply_is_metered_but_not_cached() -> None:
    # 丢弃的空回复照样计费(思考花了钱),但不进响应缓存;3 次重试同样。
    from orchestrator.llm.empty_reply import EmptyReplyError

    rows, cache, raised = await _run_with_usage(
        AIMessage(content="", usage_metadata=_USAGE, response_metadata=_STOP)
    )

    assert isinstance(raised, EmptyReplyError)
    assert [(r.input_tokens, r.output_tokens, r.usage_kind) for r in rows] == [
        (100, 2048, "conversation")
    ] * 4
    assert cache.puts == []


@pytest.mark.asyncio
async def test_truncation_after_three_mixed_retries_fails_the_run() -> None:
    # B-194 —— 每一步两种重试合计 3 次:空回复、截断交替着来,第 4 次截断照样报错,
    # 不会各算各的再多重试。第 5 条是好回答:多重试一次就会成功收尾,测试照样红。
    from orchestrator.llm.truncation import OutputTruncatedError

    llm = _ScriptedLLM(
        responses=[
            _empty_stop(),
            _cut(),
            _empty_stop(),
            _cut(),
            AIMessage(content="不该走到这里", response_metadata=_STOP),
        ]
    )
    with pytest.raises(OutputTruncatedError):
        await _invoke_scripted(llm, thread_id="t-b191-then-cut")
    assert llm.calls == 4


@pytest.mark.asyncio
async def test_empty_reply_after_three_mixed_retries_fails_the_run() -> None:
    # 反过来:截断、空回复交替,第 4 次空回复同样报错。
    from orchestrator.llm.empty_reply import EmptyReplyError

    llm = _ScriptedLLM(
        responses=[
            _cut(),
            _empty_stop(),
            _cut(),
            _empty_stop(),
            AIMessage(content="不该走到这里", response_metadata=_STOP),
        ]
    )
    with pytest.raises(EmptyReplyError):
        await _invoke_scripted(llm, thread_id="t-cut-then-b191")
    assert llm.calls == 4


@pytest.mark.asyncio
async def test_reply_with_only_unparseable_tool_calls_is_retried() -> None:
    # 图只派发 ``tool_calls``:只带解析失败的调用,原来会被当成说完了、记成空着的成功。
    invalid = {"name": "write_file", "args": "{", "id": "call_x", "error": "bad json"}
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", invalid_tool_calls=[invalid], response_metadata=_STOP),
            AIMessage(content="这次答了", response_metadata=_STOP),
        ]
    )

    state = await _invoke_scripted(llm, thread_id="t-b191-invalid")

    assert llm.calls == 2
    assert len(_empty_notices(state)) == 1
    assert state["messages"][-1].content == "这次答了"


def _filtered_count() -> float:
    return _empty_count("filtered")


@pytest.mark.asyncio
async def test_content_filtered_empty_reply_fails_without_retry() -> None:
    # 厂商内容审核拦下、一个字没回:重来也是被拦,不重试,报的是「被审核拦了」而不是「没回答」。
    before = _filtered_count()
    llm = _StreamingScriptedLLM(
        responses=[
            AIMessage(content="", response_metadata={"finish_reason": "content_filter"}),
            AIMessage(content="不该走到这里", response_metadata=_STOP),
        ]
    )

    events, row = await _run_through_sse(llm, ToolRegistry())

    errors = [e for e in events if e.event == "error"]
    assert len(errors) == 1
    assert errors[0].data["name"] == "ContentFilteredError"
    assert "内容审核拦截" in errors[0].data["message"]
    assert row is not None and row.status.value == "error"
    assert llm.calls == 1
    assert _filtered_count() == before + 1


@pytest.mark.asyncio
async def test_content_filtered_reply_is_metered_but_not_cached() -> None:
    from orchestrator.llm.empty_reply import ContentFilteredError

    rows, cache, raised = await _run_with_usage(
        AIMessage(content="", usage_metadata=_USAGE, response_metadata={"stop_reason": "refusal"})
    )

    assert isinstance(raised, ContentFilteredError)
    assert [(r.input_tokens, r.output_tokens) for r in rows] == [(100, 2048)]
    assert cache.puts == []


def _frame_shapes(events: list[Any]) -> list[tuple[str, Any]]:
    return [
        (
            e.event,
            {k: sorted(v) if isinstance(v, dict) else None for k, v in e.data.items()}
            if isinstance(e.data, dict)
            else None,
        )
        for e in events
    ]


@pytest.mark.asyncio
async def test_empty_reply_retry_takes_the_same_path_as_the_truncation_retry() -> None:
    # 丢弃那一轮与 B-158 同一条路:同样的帧、同样形状的 updates,权威帧里只有那条隐藏提示
    # (流式预览由它取代,与 B-158 一致)。
    from orchestrator.graph_builder.builder import EMPTY_REPLY_RETRY_KEY, TRUNCATION_RETRY_KEY

    shapes: dict[str, list[tuple[str, Any]]] = {}
    discarded: dict[str, Any] = {}
    for kind, first in (("b158", _cut()), ("b191", _empty_stop())):
        llm = _StreamingScriptedLLM(
            responses=[first, AIMessage(content="ok", response_metadata=_STOP)]
        )
        events, row = await _run_through_sse(llm, ToolRegistry())
        assert row is not None and row.status.value == "success"
        shapes[kind] = _frame_shapes(events)
        discarded[kind] = next(e for e in events if e.event == "updates").data["agent"]["messages"]

    assert shapes["b191"] == shapes["b158"]
    for kind, key in (("b158", TRUNCATION_RETRY_KEY), ("b191", EMPTY_REPLY_RETRY_KEY)):
        [only] = discarded[kind]
        assert only["type"] == "human"
        assert only["additional_kwargs"] == {"expert_work_hide_from_ui": True, key: True}


# ---------------------------------------------------------------------------
# B-194 —— 每一步最多重试 3 次(截断 / 空回复合计)
# ---------------------------------------------------------------------------


def _truncation_notices(state: AgentState) -> list[BaseMessage]:
    from orchestrator.graph_builder.builder import TRUNCATION_RETRY_KEY

    return [m for m in state["messages"] if m.additional_kwargs.get(TRUNCATION_RETRY_KEY)]


@pytest.mark.asyncio
async def test_three_truncations_then_a_good_reply_completes_the_run() -> None:
    # 评测实测:同一步同样的请求再发几次,有的第 2~4 次才写得下。连截 3 次、第 4 次答出来,
    # run 照常完成;3 条提示都在历史里(隐藏),被截的回答一条都不进历史。
    from orchestrator.graph_builder.builder import TRUNCATION_RETRY_KEY

    llm = _ScriptedLLM(
        responses=[_cut(), _cut(), _cut(), AIMessage(content="拆小写完了", response_metadata=_STOP)]
    )
    seen: list[Sequence[BaseMessage]] = []
    inner = llm.__call__

    async def spy(*, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]) -> AIMessage:
        seen.append(list(messages))
        return await inner(messages=messages, tools=tools)

    state = await _invoke_scripted(spy, thread_id="t-b194-three")

    assert llm.calls == 4
    assert state["messages"][-1].content == "拆小写完了"
    assert state.get("exit_reason") == "text_response"
    assert len(_truncation_notices(state)) == 3
    assert [m.content for m in state["messages"] if isinstance(m, AIMessage)] == ["拆小写完了"]
    assert all(m.additional_kwargs.get(TRUNCATION_RETRY_KEY) for m in seen[3][-3:])
    assert state["step_count"] == 4, "每次重试照旧占一步"


@pytest.mark.asyncio
async def test_fourth_consecutive_truncation_fails_the_run() -> None:
    # 重试 3 次仍截断:第 4 次报错,不再重试。第 5 条是好回答:多重试一次就会成功收尾。
    from orchestrator.llm.truncation import OutputTruncatedError

    llm = _ScriptedLLM(
        responses=[
            *(_cut() for _ in range(4)),
            AIMessage(content="不该走到这里", response_metadata=_STOP),
        ]
    )
    with pytest.raises(OutputTruncatedError):
        await _invoke_scripted(llm, thread_id="t-b194-four")
    assert llm.calls == 4


@pytest.mark.asyncio
async def test_each_step_gets_its_own_retry_budget() -> None:
    # 只数末尾连着的提示:一步成功(这里是一次工具调用)之后,下一步重新有 3 次。
    tool = _CountingTool(name="search")
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(
        responses=[
            _cut(),
            _cut(),
            _cut(),
            AIMessage(
                content="", tool_calls=[_tool_call("search", {}, "tc-1")], response_metadata=_STOP
            ),
            _cut(),
            _cut(),
            _cut(),
            AIMessage(content="两步都做完了", response_metadata=_STOP),
        ]
    )

    state = await _invoke_scripted(llm, thread_id="t-b194-reset", registry=registry, max_steps=20)

    assert llm.calls == 8
    assert tool.calls == 1
    assert state["messages"][-1].content == "两步都做完了"
    assert len(_truncation_notices(state)) == 6
