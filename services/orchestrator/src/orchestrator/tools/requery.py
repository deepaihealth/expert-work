"""B-129 —— 哪些工具的旧结果可以收成「再调一次」提示。

只读、重新调用就能拿回数据的工具:内置工具看 ``ToolSpec.is_read_only``,MCP 工具看
服务器 ``list_tools`` 标注的 ``readOnlyHint``(``tool_def.read_only is True``;没标 /
标 ``False`` 都不算,与 B-122 给子智能体放行的判据同源)。

刻意不去改 ``ToolSpec.is_read_only`` 来表达这件事:它还决定失败自动重试
(``error_classifier``)与并行调度(``scheduling``),把 MCP 工具标成只读会顺带改掉这两处。
"""

from __future__ import annotations

from orchestrator.context.tool_result_prune import NEVER_PRUNE_TOOLS
from orchestrator.tools.mcp import MCPTool
from orchestrator.tools.registry import ToolRegistry

__all__ = ["requery_tool_names"]


def requery_tool_names(registry: ToolRegistry) -> frozenset[str]:
    names: set[str] = set()
    for spec in registry.all_specs():
        tool = registry.get(spec.name)
        read_only = (
            tool.tool_def.read_only is True if isinstance(tool, MCPTool) else spec.is_read_only
        )
        if read_only:
            names.add(spec.name)
    return frozenset(names - NEVER_PRUNE_TOOLS)
