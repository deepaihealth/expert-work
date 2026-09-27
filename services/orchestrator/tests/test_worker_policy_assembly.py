"""B-122 —— worker 构建(ToolEnv.worker_policy=True)的工具表。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest

from expert_work.protocol import ArgBindingSpec, BuiltinToolSpec, HTTPToolSpec, MCPToolSpec
from orchestrator.tools import (
    MCPServerPool,
    MCPToolDef,
    RecordingMCPClient,
    ToolEnv,
    build_tool_registry,
)

pytestmark = pytest.mark.asyncio

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"employee_code": {"type": "string"}, "text": {"type": "string"}},
}


async def _allow_all(_tenant_id: Any) -> list[str]:
    return []


async def _env(*, worker: bool) -> ToolEnv:
    pool = MCPServerPool()
    await pool.add(
        "deepcare",
        RecordingMCPClient(
            tools=(
                MCPToolDef(name="form_list", description="r", input_schema=_SCHEMA, read_only=True),
                MCPToolDef(
                    name="cpwx_send", description="w", input_schema=_SCHEMA, read_only=False
                ),
            )
        ),
    )
    env = ToolEnv(
        sandbox_runtime=Mock(),
        artifact_store=Mock(),
        workspace_store=Mock(),
        allowlist_provider=_allow_all,
        mcp_pool=pool,
    )
    return replace(env, worker_policy=worker)


_TOOLS = [
    BuiltinToolSpec(name="save_artifact"),  # 显式声明也要剥(Review Focus 1)
    HTTPToolSpec(),
    MCPToolSpec(
        servers=["deepcare"],
        arg_bindings=[
            ArgBindingSpec(
                server="deepcare", tool="cpwx_send", args={"employee_code": "employee_code"}
            )
        ],
    ),
]


async def test_main_build_is_unchanged() -> None:
    registry = await build_tool_registry(_TOOLS, tool_env=await _env(worker=False))
    assert "save_artifact" in registry
    assert "mcp__deepcare__cpwx_send" in registry
    assert "POST" in registry.get_required("http").spec.parameters["properties"]["method"]["enum"]


async def test_worker_build_drops_delivery_write_mcp_and_write_http(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        registry = await build_tool_registry(_TOOLS, tool_env=await _env(worker=True))
    assert "save_artifact" not in registry
    assert "list_artifacts" in registry  # 读产物不受限
    assert "write_file" in registry  # 工作区照常可写
    assert "mcp__deepcare__form_list" in registry
    assert "mcp__deepcare__cpwx_send" not in registry
    assert registry.get_required("http").spec.parameters["properties"]["method"]["enum"] == [
        "GET",
        "HEAD",
        "OPTIONS",
    ]
    # 被策略滤掉的写工具上的绑定,不能报「绑定落空」(Review Focus 5)
    assert registry.unmatched_arg_bindings() == ()
    assert not [r for r in caplog.records if "arg_binding_unmatched" in r.getMessage()]


async def test_worker_build_skips_declared_ask_for_approval() -> None:
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="ask_for_approval")], tool_env=await _env(worker=True)
    )
    assert "ask_for_approval" not in registry
