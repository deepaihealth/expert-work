from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.tools import ToolRegistry, ToolSpec
from orchestrator.tools.mcp import MCPTool, MCPToolDef, RecordingMCPClient
from orchestrator.tools.requery import requery_tool_names


@dataclass
class _Builtin:
    spec: ToolSpec

    async def call(self, args: Any, *, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _builtin(name: str, *, read_only: bool) -> _Builtin:
    return _Builtin(ToolSpec(name=name, description="d", parameters={}, is_read_only=read_only))


def _mcp_tool(name: str, read_only: bool | None) -> MCPTool:
    tool_def = MCPToolDef(name=name, description="d", input_schema={}, read_only=read_only)
    return MCPTool(client=RecordingMCPClient(), tool_def=tool_def, server_name="srv")


def test_builtin_read_only_tools_are_listed() -> None:
    reg = ToolRegistry()
    reg.register(_builtin("lookup_x", read_only=True))
    reg.register(_builtin("write_x", read_only=False))
    assert requery_tool_names(reg) == frozenset({"lookup_x"})


def test_mcp_read_only_hint_true_is_listed() -> None:
    reg = ToolRegistry()
    tool = _mcp_tool("fetch_record", True)
    reg.register(tool)
    assert tool.spec.name in requery_tool_names(reg)


def test_mcp_unannotated_or_write_tools_excluded() -> None:
    reg = ToolRegistry()
    reg.register(_mcp_tool("maybe", None))
    reg.register(_mcp_tool("writes", False))
    assert requery_tool_names(reg) == frozenset()


def test_ask_image_excluded_even_if_read_only() -> None:
    reg = ToolRegistry()
    reg.register(_builtin("ask_image", read_only=True))
    assert requery_tool_names(reg) == frozenset()


def test_deferred_tools_are_listed_too() -> None:
    reg = ToolRegistry()
    reg.register(_builtin("lookup_x", read_only=True), deferred=True)
    assert requery_tool_names(reg) == frozenset({"lookup_x"})
