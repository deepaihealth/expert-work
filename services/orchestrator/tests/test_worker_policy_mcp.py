"""B-122 —— MCP 读写标注带进 MCPToolDef;worker 只注册只读的。"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from orchestrator.tools import MCPToolDef, RecordingMCPClient
from orchestrator.tools.mcp import _materialize_tool_defs, register_mcp_tools
from orchestrator.tools.registry import ToolRegistry


class _Ann:
    def __init__(self, read_only: Any) -> None:
        self.readOnlyHint = read_only


class _Raw:
    def __init__(self, name: str, annotations: Any = None) -> None:
        self.name = name
        self.description = name
        self.inputSchema: dict[str, Any] = {"type": "object", "properties": {}}
        self.annotations = annotations


def test_materialize_reads_read_only_hint() -> None:
    defs = _materialize_tool_defs(
        [
            _Raw("q", _Ann(True)),
            _Raw("w", _Ann(False)),
            _Raw("none"),
            _Raw("as_dict", {"readOnlyHint": True}),
            _Raw("string_true", _Ann("true")),
            _Raw("one", _Ann(1)),
        ],
        server="s",
    )
    by = {d.name: d.read_only for d in defs}
    # 只有「对象属性 == True 且是 bool」才算只读;字典形态、字符串、1 都当没标
    assert by == {
        "q": True,
        "w": False,
        "none": None,
        "as_dict": None,
        "string_true": None,
        "one": None,
    }


def _defs() -> tuple[MCPToolDef, ...]:
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    return (
        MCPToolDef(name="read_it", description="r", input_schema=schema, read_only=True),
        MCPToolDef(name="send_it", description="w", input_schema=schema, read_only=False),
        MCPToolDef(name="unknown", description="u", input_schema=schema),
    )


@pytest.mark.asyncio
async def test_default_registers_everything() -> None:
    registry = ToolRegistry()
    names = await register_mcp_tools(
        server_name="srv", client=RecordingMCPClient(tools=_defs()), registry=registry
    )
    assert len(names) == 3


@pytest.mark.asyncio
async def test_read_only_only_keeps_annotated_read_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ToolRegistry()
    with caplog.at_level(logging.INFO, logger="orchestrator.tools.mcp"):
        names = await register_mcp_tools(
            server_name="srv",
            client=RecordingMCPClient(tools=_defs()),
            registry=registry,
            read_only_only=True,
        )
    assert names == ["mcp__srv__read_it"]
    assert "mcp__srv__send_it" not in registry
    assert "mcp__srv__unknown" not in registry
    line = next(
        r.getMessage() for r in caplog.records if "worker_read_only_filter" in r.getMessage()
    )
    assert "kept=1" in line and "dropped_write=1" in line and "dropped_unannotated=1" in line
