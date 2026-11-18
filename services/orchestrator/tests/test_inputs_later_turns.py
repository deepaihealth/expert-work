# ruff: noqa: RUF001 — 断言的就是面向模型的中文全角标点
"""B-317 复审 M1 —— 多轮追问:后一轮没传输入时,模型用前一轮回执里给出的真实路径接着读。

``inputs/current/`` 永远指本轮。第 1 轮用短路径读的文件,回执末尾(平台 notice,围栏外)
说一次真实路径;第 2 轮没有输入,``inputs/current/`` 报错,模型照回执里的路径读得到。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.multimodal import NasWorkspaceImageResolver
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.vision import AskImageTool

from .test_input_url_guard_wiring import _ScriptedLLM, _tc, _tool_messages

pytestmark = pytest.mark.asyncio

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_AGENT = "report-aaaaaaaa"
_DIGEST = "b" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
_LATER = re.compile(r"（之后的轮次请用 (\S+)）")


@dataclass
class _VL:
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls.append(list(messages))
        return AIMessage(content="血糖 9.1")


def _plant(root: Path, run: UUID) -> None:
    base = root / str(_TENANT) / str(_USER) / "agents" / _AGENT / "inputs"
    (base / "cache").mkdir(parents=True, exist_ok=True)
    (base / "cache" / f"{_DIGEST}.png").write_bytes(_PNG)
    (base / str(run)).mkdir()
    (base / str(run) / "lab.png").symlink_to(f"../cache/{_DIGEST}.png")


async def _turn(
    root: Path, *, run: UUID, inputs: Mapping[str, Any] | None, path: str
) -> ToolMessage:
    registry = ToolRegistry()
    registry.register(
        AskImageTool(
            vl_caller=_VL(),
            image_resolver=NasWorkspaceImageResolver(root=root),
            workspace_store=NasWorkspaceStore(root=str(root)),
        )
    )
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="", tool_calls=[_tc("ask_image", {"path": path, "question": "?"}, "tc-1")]
            ),
            AIMessage(content="done"),
        ]
    )
    cfg: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "run_id": str(run),
            "agent_key": _AGENT,
            **({PROMPT_INPUTS_KEY: dict(inputs)} if inputs else {}),
        }
    }
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="看看化验单")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    return _tool_messages(state)["tc-1"]


async def test_a_later_turn_without_inputs_reads_the_file_by_the_path_the_receipt_gave(
    tmp_path: Path,
) -> None:
    first = uuid4()
    _plant(tmp_path, first)

    turn1 = await _turn(
        tmp_path,
        run=first,
        inputs={"lab": "https://x.invalid/lab.png"},
        path="inputs/current/lab.png",
    )
    assert turn1.status != "error", turn1.content
    found = _LATER.search(str(turn1.content))
    assert found is not None, turn1.content
    assert found.group(1) == f"inputs/{first}/lab.png"

    # 第 2 轮:没有输入。短路径报错,且报错指向回执里的那句;回执给的路径读得到。
    again = await _turn(tmp_path, run=uuid4(), inputs=None, path="inputs/current/lab.png")
    assert again.status == "error"
    assert "回执末尾的「之后的轮次请用 …」" in str(again.content)
    turn2 = await _turn(tmp_path, run=uuid4(), inputs=None, path=found.group(1))
    assert turn2.status != "error", turn2.content
    assert "血糖 9.1" in str(turn2.content)
    assert _LATER.search(str(turn2.content)) is None  # 已经是真实路径,不再补一句


@dataclass
class _PathTool:
    name: str
    calls: list[str] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="d", is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls.append(str(args["path"]))
        return ToolResult(content="ok")


@pytest.mark.parametrize(
    ("written", "noted"),
    [
        ("inputs/current/report.pdf", True),
        ("$EXPERT_WORK_INPUTS_DIR/report.pdf", True),
        ("notes/a.md", False),
    ],
)
async def test_every_path_tool_states_the_real_path_once_after_a_short_one(
    written: str, noted: bool
) -> None:
    run = uuid4()
    tool = _PathTool("read_document")
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc("read_document", {"path": written}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    cfg: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "tenant_id": str(_TENANT),
            "run_id": str(run),
            "agent_key": _AGENT,
            PROMPT_INPUTS_KEY: {"report": "https://x.invalid/r.pdf"},
        }
    }
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="x")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    content = str(_tool_messages(state)["tc-1"].content)
    note = f"（之后的轮次请用 inputs/{run}/report.pdf）"
    assert (note in content) is noted
    assert content.count("之后的轮次") == (1 if noted else 0)
