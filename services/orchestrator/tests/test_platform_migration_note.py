"""B-262 P3 —— 迁移的那一轮,平台段里给模型一句一次性说明。

控制面在起新 run 之前把老对话迁到对话视图时写 ``configurable[WORKSPACE_MIGRATED_KEY]``;
``agent_node`` 每一步把说明并进 ``<platform-context>``(只进提示词视图、不落库),于是只在这一轮
出现,之后的 run 没有。委派子代(``_child_config`` 不传这个键,``child_run`` 另外挡)不看到。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig

from orchestrator.graph_builder.builder import WORKSPACE_MIGRATION_NOTE
from orchestrator.tools.registry import (
    WORKSPACE_MIGRATED_KEY,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)

from .test_figure_block_injection import _graph, _RecordingLLM, _tool_turn


@dataclass
class _Echo:
    name: str = "echo"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="echo", is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _text(messages: list[BaseMessage]) -> str:
    return "\n".join(str(m.content) for m in messages)


async def _run(llm: _RecordingLLM, registry: ToolRegistry, **configurable: Any) -> None:
    from langchain_core.messages import HumanMessage

    config: RunnableConfig = {
        "configurable": {"thread_id": str(uuid4()), "tenant_id": str(uuid4()), **configurable}
    }
    async with _graph(llm, registry=registry) as compiled:
        await compiled.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 5},
            config=config,
        )


@pytest.mark.parametrize(
    ("configurable", "expected"),
    [
        ({WORKSPACE_MIGRATED_KEY: True}, 1),
        ({}, 0),
        ({WORKSPACE_MIGRATED_KEY: True, "child_run": True}, 0),
    ],
    ids=["migrated-run", "ordinary-run", "child-run"],
)
async def test_the_note_appears_once_per_prompt_only_on_the_migrating_run(
    configurable: dict[str, Any], expected: int
) -> None:
    registry = ToolRegistry()
    registry.register(_Echo())
    llm = _RecordingLLM(responses=[_tool_turn("echo")])

    await _run(llm, registry, **configurable)

    assert len(llm.seen_prompts) == 2  # 工具调用前后两步
    for prompt in llm.seen_prompts:
        assert _text(prompt).count(WORKSPACE_MIGRATION_NOTE) == expected
        if expected:
            assert "<platform-context>" in str(prompt[-1].content)


def test_the_note_is_plain_platform_text() -> None:
    """平台文字:英文、不含租户内容,讲清三件事。"""
    note = WORKSPACE_MIGRATION_NOTE
    assert note.isascii()
    for fragment in ("/workspace", "/agent/", "uploads", "inputs", "tool results"):
        assert fragment in note
