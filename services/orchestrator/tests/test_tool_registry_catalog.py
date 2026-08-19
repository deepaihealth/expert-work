"""PR-A.3 — ``ToolRegistry.catalog()``:控制面 Schema tab 要的「整个注册表」投影。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestrator.tools.registry import (
    ToolCatalogEntry,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class _T:
    def __init__(self, name: str, *, from_skill: str | None = None) -> None:
        self._spec = ToolSpec(
            name=name,
            description=f"desc {name}",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            from_skill=from_skill,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="")


def test_catalog_lists_every_tool_in_registration_order_with_source_and_deferred() -> None:
    reg = ToolRegistry()
    reg.register(_T("bash"))
    reg.register(_T("mcp__gh__create_issue"), source="mcp:gh", deferred=True)
    reg.register(_T("skill_tool", from_skill="writer"))

    cat = reg.catalog()

    assert [c.name for c in cat] == ["bash", "mcp__gh__create_issue", "skill_tool"]
    assert cat[0] == ToolCatalogEntry(
        name="bash",
        description="desc bash",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        source="builtin",
        from_skill=None,
        deferred=False,
    )
    assert cat[1].source == "mcp:gh" and cat[1].deferred is True
    assert cat[2].from_skill == "writer"
    # specs() 不含 deferred,catalog() 含 —— 两者的差就是 deferred 集合。
    assert {c.name for c in cat if c.deferred} == {c.name for c in cat} - {
        s.name for s in reg.specs()
    }
