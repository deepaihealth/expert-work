"""厂商严格 schema 校验守卫 —— B-34。

真栈实证(2026-08-28,run 0de32ed5):moonshot(kimi)对 tool JSON schema
做严格校验,``enum`` 出现在没有显式 ``"type"`` 的节点上直接 400
``is not a valid moonshot flavored json schema``,整个 run 报错。凡是拼给
LLM 的 schema 不能赌厂商宽松(同 MCP 工具名 wire-safe 的教训)——这里对
**全部内置工具** 的 parameters 做递归扫描:任何含 ``enum`` 的 schema 节点
必须同时声明 ``type``。
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.tools.manage_task import _PARAMETERS as MANAGE_TASK_PARAMETERS
from orchestrator.tools.update_plan import UpdatePlanTool


def _nodes_with_enum(node: Any, path: str = "$") -> list[tuple[str, dict]]:
    """Walk a JSON-schema fragment, yielding every dict node that has ``enum``."""
    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if "enum" in node:
            found.append((path, node))
        for key, child in node.items():
            found.extend(_nodes_with_enum(child, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, child in enumerate(node):
            found.extend(_nodes_with_enum(child, f"{path}[{i}]"))
    return found


def _assert_enum_nodes_typed(parameters: dict, tool_name: str) -> None:
    offenders = [path for path, node in _nodes_with_enum(parameters) if "type" not in node]
    assert not offenders, (
        f"{tool_name}: schema nodes with 'enum' but no 'type' "
        f"(moonshot strict validation rejects these): {offenders}"
    )


@pytest.mark.parametrize(
    ("tool_name", "parameters"),
    [
        ("update_plan", UpdatePlanTool().spec.parameters),
        ("manage_task", MANAGE_TASK_PARAMETERS),
    ],
)
def test_builtin_tool_schema_enum_nodes_declare_type(tool_name: str, parameters: dict) -> None:
    _assert_enum_nodes_typed(parameters, tool_name)
