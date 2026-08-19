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


def test_catalog_parameters_do_not_share_nested_objects_with_the_live_spec() -> None:
    """PR-A.3 follow-up(终审 Minor 8)—— ``ToolCatalogEntry`` 是 frozen,但
    ``parameters`` 只是浅拷贝时嵌套 dict 仍与喂给 LLM 的 ``ToolSpec`` 同一对象:
    消费者改一下投影(比如序列化前加注解)就会污染真 schema。锁成深拷贝。"""
    reg = ToolRegistry()
    tool = _T("bash")
    reg.register(tool)
    entry = reg.catalog()[0]

    assert entry.parameters == tool.spec.parameters
    assert entry.parameters is not tool.spec.parameters
    assert entry.parameters["properties"] is not tool.spec.parameters["properties"]
    # 改投影,真 spec 不动。
    props = entry.parameters["properties"]
    assert isinstance(props, dict)
    props["q"] = {"type": "integer"}
    assert tool.spec.parameters["properties"]["q"] == {"type": "string"}
