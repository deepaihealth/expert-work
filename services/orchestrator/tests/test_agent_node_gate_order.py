"""Stream RT-2 PR-1 — order-pin for agent_node's four context gates.

``builder.agent_node`` applies, in a fixed order that downstream
invariants depend on: CM-12 tool-result pruner (cheapest, least lossy —
first, so the coarser gates re-estimate against a smaller prompt) →
CM-2 working window → injection (plan / memories / recovery advisory —
this turn's guidance, must land after the trims so it always reaches
the LLM) → L2 compressor (last, so it sees the final prompt view).

deer-flow #3809 showed how a magic-index refactor can silently reorder
such a cascade; the tracing doubles here pin both the call sequence and
the between-gate payloads so any reorder fails loudly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import HIDE_FROM_UI, INPUTS_BLOCK_MARK
from expert_work.protocol import MemoryItem
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.context import PruneResult, TrimResult
from orchestrator.tools.registry import ToolSpec


@dataclass
class _ScriptedLLM:
    """Records every prompt and replies without tool calls so the run
    ends after one agent step."""

    seen_prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del tools
        self.seen_prompts.append(list(messages))
        return AIMessage(content="done")


@dataclass
class _TracingPruner:
    """CM-12 double — records its slot in the shared order log plus the
    exact prompt view it received, then appends a breadcrumb."""

    order: list[str]
    seen: list[list[BaseMessage]] = field(default_factory=list)

    def apply(self, messages: Sequence[BaseMessage]) -> PruneResult:
        self.order.append("pruner")
        self.seen.append(list(messages))
        return PruneResult(
            messages=[*messages, HumanMessage(content="[pruner-mark]")],
            pruned_count=0,
        )


@dataclass
class _TracingWindow:
    """CM-2 double — same tracing contract as the pruner."""

    order: list[str]
    seen: list[list[BaseMessage]] = field(default_factory=list)

    def apply(self, messages: Sequence[BaseMessage]) -> TrimResult:
        self.order.append("window")
        self.seen.append(list(messages))
        return TrimResult(
            messages=[*messages, HumanMessage(content="[window-mark]")],
            dropped_turns=0,
        )


@dataclass
class _TracingCompressor:
    """L2 double — always claims the prompt is over threshold so the
    compress step fires, then appends its breadcrumb."""

    order: list[str]
    seen: list[list[BaseMessage]] = field(default_factory=list)

    def should_compress(
        self, messages: Sequence[BaseMessage], *, reserved: Sequence[BaseMessage] = ()
    ) -> bool:
        del messages, reserved
        return True

    async def compress(
        self,
        messages: Sequence[BaseMessage],
        *,
        on_pre_compaction: object = None,
        on_compacted: object = None,
        streak_key: str | None = None,
        reserved: Sequence[BaseMessage] = (),
    ) -> list[BaseMessage]:
        del on_pre_compaction, on_compacted, streak_key, reserved
        self.order.append("compressor")
        self.seen.append(list(messages))
        return [*messages, HumanMessage(content="[compressor-mark]")]


def _memory(content: str) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content=content,
        embedding=(0.0,),
    )


def _texts(messages: Sequence[BaseMessage]) -> str:
    return "\n".join(str(m.content) for m in messages)


@pytest.mark.asyncio
async def test_gate_order_pruner_window_injection_compressor() -> None:
    order: list[str] = []
    pruner = _TracingPruner(order)
    window = _TracingWindow(order)
    compressor = _TracingCompressor(order)
    llm = _ScriptedLLM()

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=ToolRegistry(),
                tool_result_pruner=pruner,
                working_window=window,
                context_compressor=compressor,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="task")],
                "recalled_memories": [_memory("remember the alamo")],
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    # The gates fire exactly once each, in the pinned order.
    assert order == ["pruner", "window", "compressor"]

    # Pruner runs FIRST: the raw history — no gate marks, no injection.
    pruner_view = _texts(pruner.seen[0])
    assert "[window-mark]" not in pruner_view
    assert "Relevant memories" not in pruner_view

    # Window runs on the pruner's output, still before injection.
    window_view = _texts(window.seen[0])
    assert "[pruner-mark]" in window_view
    assert "Relevant memories" not in window_view

    # Injection (memories) lands AFTER the window and BEFORE the
    # compressor — the compressor is the only gate that sees it.
    compressor_view = _texts(compressor.seen[0])
    assert "[pruner-mark]" in compressor_view
    assert "[window-mark]" in compressor_view
    assert "remember the alamo" in compressor_view

    # The LLM receives the compressor's output — the last gate's view.
    assert "[compressor-mark]" in _texts(llm.seen_prompts[0])


# --------------------------------------------------------------------- B-73 ③


@dataclass
class _DroppingCompressor:
    """L2 double that behaves like summarise-the-middle: it keeps the head and
    the last ``tail_keep`` messages and replaces everything between them with a
    summary. That is the real shape — ``tail_keep`` counts MESSAGES, so a long
    turn's hidden scaffolding falls into the middle."""

    tail_keep: int = 1
    seen: list[list[BaseMessage]] = field(default_factory=list)

    def should_compress(
        self, messages: Sequence[BaseMessage], *, reserved: Sequence[BaseMessage] = ()
    ) -> bool:
        del messages, reserved
        return True

    async def compress(
        self,
        messages: Sequence[BaseMessage],
        *,
        on_pre_compaction: object = None,
        on_compacted: object = None,
        streak_key: str | None = None,
        reserved: Sequence[BaseMessage] = (),
    ) -> list[BaseMessage]:
        del on_pre_compaction, on_compacted, streak_key, reserved
        self.seen.append(list(messages))
        head, tail = list(messages[:1]), list(messages[-self.tail_keep :])
        return [*head, HumanMessage(content="<context-summary>…</context-summary>"), *tail]


def _inputs_block(text: str) -> HumanMessage:
    """``control_plane.inputs_block.inputs_block_message`` 的形状 —— 标记住在
    common,所以这里不跨服务 import。"""
    return HumanMessage(
        content=text,
        additional_kwargs={HIDE_FROM_UI: True, INPUTS_BLOCK_MARK: True},
    )


async def _run_with_compressor(
    compressor: _DroppingCompressor, messages: list[BaseMessage]
) -> _ScriptedLLM:
    llm = _ScriptedLLM()
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=ToolRegistry(),
                context_compressor=compressor,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {"messages": messages, "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    return llm


@pytest.mark.asyncio
async def test_the_latest_inputs_block_survives_compression() -> None:
    """B-73 ③ —— 这一段是模型找输入文件用的地图(变量名 + 路径)。被摘要掉之后
    模型只能退回去手抄上文里的长串,正是 B-61 / B-67 要消灭的病。"""
    compressor = _DroppingCompressor()
    llm = await _run_with_compressor(
        compressor,
        [
            HumanMessage(content="做一版海报"),
            _inputs_block("[本轮输入] org_logo -> $EXPERT_WORK_INPUTS_DIR/org_logo.png"),
            AIMessage(content="好的"),
            HumanMessage(content="换个配色"),
        ],
    )
    # 压缩器真的把它丢了 —— 否则这条测试在不可能失败的条件下通过。
    assert "$EXPERT_WORK_INPUTS_DIR" in _texts(compressor.seen[0])
    prompt = llm.seen_prompts[0]
    assert "$EXPERT_WORK_INPUTS_DIR/org_logo.png" in _texts(prompt)
    # 原件放回去,不是摘要里的转述。
    assert any(m.additional_kwargs.get(INPUTS_BLOCK_MARK) for m in prompt)


@pytest.mark.asyncio
async def test_a_kept_inputs_block_is_not_duplicated() -> None:
    compressor = _DroppingCompressor(tail_keep=3)
    llm = await _run_with_compressor(
        compressor,
        [
            HumanMessage(content="做一版海报"),
            _inputs_block("[本轮输入] org_logo -> $EXPERT_WORK_INPUTS_DIR/org_logo.png"),
            AIMessage(content="好的"),
        ],
    )
    blocks = [m for m in llm.seen_prompts[0] if m.additional_kwargs.get(INPUTS_BLOCK_MARK)]
    assert len(blocks) == 1


@pytest.mark.asyncio
async def test_only_the_latest_inputs_block_comes_back() -> None:
    """更早轮次的段说的是已经过期的路径(那些 run 目录会被 janitor 回收),压掉是对的。"""
    compressor = _DroppingCompressor()
    llm = await _run_with_compressor(
        compressor,
        [
            HumanMessage(content="第一轮"),
            _inputs_block("[本轮输入] 旧路径 $EXPERT_WORK_INPUTS_DIR/old.png"),
            AIMessage(content="ok"),
            HumanMessage(content="第二轮"),
            _inputs_block("[本轮输入] 新路径 $EXPERT_WORK_INPUTS_DIR/new.png"),
            AIMessage(content="ok"),
        ],
    )
    text = _texts(llm.seen_prompts[0])
    assert "new.png" in text
    assert "old.png" not in text
