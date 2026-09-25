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


async def _run_through_sse(llm: _ScriptedLLM, registry: ToolRegistry) -> tuple[list[Any], Any]:
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
    # 思考模型截断的实测形态:额度全给了思考,正文为空。
    before = _truncated_count("false")
    llm = _StreamingScriptedLLM(
        responses=[AIMessage(content="", response_metadata={"finish_reason": "length"})]
    )

    events, row = await _run_through_sse(llm, ToolRegistry())

    errors = [e for e in events if e.event == "error"]
    assert len(errors) == 1
    assert errors[0].data["name"] == "OutputTruncatedError"
    assert "模型输出被截断" in errors[0].data["message"]
    assert "2048" in errors[0].data["message"]
    assert row is not None and row.error is not None and "模型输出被截断" in row.error
    assert _truncated_count("false") == before + 1


@pytest.mark.asyncio
async def test_truncated_tool_call_fails_before_dispatch() -> None:
    # 截断时最后一个工具调用的参数必然被截:不能把残缺参数派给工具。
    llm = _StreamingScriptedLLM(
        responses=[
            AIMessage(
                content="好的,我来写文件",
                tool_calls=[_tool_call("write_file", {}, "tc-1")],
                response_metadata={"stop_reason": "max_tokens"},
            )
        ]
    )
    tool = _CountingTool(name="write_file")
    registry = ToolRegistry()
    registry.register(tool)

    events, _ = await _run_through_sse(llm, registry)

    assert [e.data["name"] for e in events if e.event == "error"] == ["OutputTruncatedError"]
    assert tool.calls == 0


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

    llm = _ScriptedLLM(
        responses=[AIMessage(content="", response_metadata={"finish_reason": "length"})]
    )
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
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                response_metadata={"finish_reason": "length", SERVED_BY_KEY: "qwen:qwen3.6-plus#1"},
            )
        ]
    )
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
    assert _count("qwen", "qwen3.6-plus", "false") == before + 1


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
                llm_caller=_ScriptedLLM(responses=[response]),
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
    ]
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
