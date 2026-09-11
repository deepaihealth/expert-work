"""Unit tests for ``_build_tool_context`` — Stream J.4 (cancellation token)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.cancellation import CANCELLATION_TOKEN_KEY, CancellationToken
from orchestrator import ToolContext, ToolResult, ToolSpec
from orchestrator.graph_builder.builder import _build_tool_context, _invoke_tool
from orchestrator.tools._worker_events import WORKER_EVENT_SINK_KEY


def test_build_tool_context_carries_run_cancellation_token() -> None:
    # The run's CancellationToken in config["configurable"] reaches the
    # ToolContext — so a tool (J.4 SubAgentTool) can thread it into work
    # it spawns.
    token = CancellationToken()
    config: RunnableConfig = {"configurable": {CANCELLATION_TOKEN_KEY: token}}

    ctx = _build_tool_context(config)

    assert ctx.cancellation_token is token


def test_build_tool_context_supplies_fresh_token_when_absent() -> None:
    # No token in config (dev / unit-test path) → a fresh, never-cancelled
    # token, so ``ctx.cancellation_token`` is always populated.
    ctx = _build_tool_context({"configurable": {}})

    assert isinstance(ctx.cancellation_token, CancellationToken)
    assert not ctx.cancellation_token.cancelled()


# --- B2 worker_event_sink / tool_call_id ------------------------------------


@pytest.mark.asyncio
async def test_tool_context_reads_worker_event_sink_from_config() -> None:
    async def _sink(frame: dict[str, Any]) -> None:
        del frame
        return None

    config: RunnableConfig = {"configurable": {WORKER_EVENT_SINK_KEY: _sink}}
    ctx = _build_tool_context(config)
    assert ctx.worker_event_sink is _sink


def test_tool_context_worker_event_sink_defaults_none() -> None:
    ctx = _build_tool_context({"configurable": {}})
    assert ctx.worker_event_sink is None
    assert ctx.tool_call_id is None


def test_tool_context_ignores_non_callable_worker_sink() -> None:
    ctx = _build_tool_context({"configurable": {WORKER_EVENT_SINK_KEY: "not-callable"}})
    assert ctx.worker_event_sink is None


@dataclass
class _ProbeTool:
    """Minimal ``Tool`` stub — mirrors ``_SchemaTool`` in
    ``test_tool_arg_validation.py``, parameterized over ``call`` so a test
    can observe the ``ctx`` it's dispatched with."""

    name: str
    call_fn: Any
    parameters: Mapping[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="probe", parameters=self.parameters)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        return await self.call_fn(args, ctx=ctx)


def _make_tool(*, name: str, call: Any) -> _ProbeTool:
    return _ProbeTool(name=name, call_fn=call)


@pytest.mark.asyncio
async def test_invoke_tool_threads_tool_call_id_into_ctx() -> None:
    seen: dict[str, Any] = {}

    async def _call(args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args
        seen["tool_call_id"] = ctx.tool_call_id
        return ToolResult(content="ok")

    tool = _make_tool(name="probe", call=_call)  # 按文件内现有工具构造惯例
    await _invoke_tool(tool, {}, "call-42", ToolContext())
    assert seen["tool_call_id"] == "call-42"


# --- 工作区分层 agent_key ----------------------------------------------------


def test_agent_key_lifts_from_configurable() -> None:
    """``agent_key`` 从 ``configurable`` 抬进 ``ToolContext`` —— 工具层判归属的唯一来源。"""
    ctx = _build_tool_context(
        {"configurable": {"tenant_id": str(uuid4()), "agent_key": "ai-health-plan-1a2b3c4d"}}
    )

    assert ctx.agent_key == "ai-health-plan-1a2b3c4d"


def test_agent_key_defaults_to_empty_when_absent() -> None:
    """没绑 agent 的调用(合成 eval / 未升级的调用方)拿到空串,不是 ``None``。

    与 ``agent_key_envs("")`` 的既有「空串=不注入」语义对齐,工具层据此走回落。
    """
    ctx = _build_tool_context({"configurable": {"tenant_id": str(uuid4())}})

    assert ctx.agent_key == ""


def test_agent_key_rejects_non_string() -> None:
    """``configurable`` 是不可信输入(与 ``_string_list`` / ``_parse_uuid`` 同一姿态)。"""
    ctx = _build_tool_context({"configurable": {"agent_key": 12345}})

    assert ctx.agent_key == ""
