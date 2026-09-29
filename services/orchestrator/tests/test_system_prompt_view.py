"""B-128 —— prompt 视图只保留最新一份系统提示词。

入口每一轮都往检查点追加一条 ``SystemMessage``(当轮渲染的系统提示词),而
``coalesce_system_messages`` 把所有 SystemMessage 按顺序拼成一条 —— 第 N 轮的
请求里就有 N 份几乎相同的提示词,且每轮新追加的那份让前缀缓存在上一份末尾断开。
``keep_latest_system_prompt`` 只改 prompt 视图:留最后一条非摘要 SystemMessage,
放到下标 0;压缩器的 ``<context-summary>`` 原位不动;其余消息相对顺序不变。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.context.system_prompt_view import keep_latest_system_prompt


def _contents(messages: Sequence[BaseMessage]) -> list[str]:
    return [str(m.content) for m in messages]


def test_single_turn_is_returned_unchanged() -> None:
    messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    assert keep_latest_system_prompt(messages) == messages


def test_no_system_message_is_returned_unchanged() -> None:
    messages = [HumanMessage(content="hi"), AIMessage(content="yo")]
    assert keep_latest_system_prompt(messages) == messages


def test_multi_turn_keeps_only_latest_prompt_at_head() -> None:
    messages = [
        SystemMessage(content="sys v1"),
        HumanMessage(content="u1"),
        AIMessage(content="a1"),
        SystemMessage(content="sys v1"),
        HumanMessage(content="u2"),
        AIMessage(content="a2"),
        SystemMessage(content="sys v2"),
        HumanMessage(content="u3"),
    ]
    out = keep_latest_system_prompt(messages)
    assert _contents(out) == ["sys v2", "u1", "a1", "u2", "a2", "u3"]
    assert out[0] is messages[6]


def test_lone_prompt_not_at_head_moves_to_head() -> None:
    messages = [HumanMessage(content="u1"), SystemMessage(content="sys")]
    assert _contents(keep_latest_system_prompt(messages)) == ["sys", "u1"]


def test_context_summary_is_kept_in_place_and_never_chosen_as_prompt() -> None:
    summary = SystemMessage(content="<context-summary>older turns</context-summary>")
    messages = [
        SystemMessage(content="sys v1"),
        summary,
        HumanMessage(content="u2"),
        SystemMessage(content="sys v2"),
        HumanMessage(content="u3"),
    ]
    out = keep_latest_system_prompt(messages)
    assert _contents(out) == ["sys v2", summary.content, "u2", "u3"]


def test_input_is_not_mutated() -> None:
    messages = [
        SystemMessage(content="s1"),
        HumanMessage(content="u1"),
        SystemMessage(content="s2"),
        HumanMessage(content="u2"),
    ]
    snapshot = list(messages)
    out = keep_latest_system_prompt(messages)
    assert messages == snapshot
    assert out is not messages
    assert keep_latest_system_prompt(snapshot[:2]) is not snapshot


# ------------------------------------------------------------------ wiring


@dataclass
class _RecordingLLM:
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        self.seen.append(list(messages))
        return AIMessage(content="done")


@pytest.mark.asyncio
async def test_agent_node_sends_one_prompt_and_keeps_checkpoint_intact() -> None:
    history: list[BaseMessage] = [
        SystemMessage(content="sys v1"),
        HumanMessage(content="u1"),
        AIMessage(content="a1"),
        SystemMessage(content="sys v2"),
        HumanMessage(content="u2"),
    ]
    llm = _RecordingLLM()
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry())
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        state = await compiled.ainvoke(
            {"messages": history, "step_count": 0, "max_steps": 5}, config=cfg
        )
    systems = [m for m in llm.seen[0] if isinstance(m, SystemMessage)]
    assert _contents(systems) == ["sys v2"]
    assert isinstance(llm.seen[0][0], SystemMessage)
    saved_systems = [m for m in state["messages"] if isinstance(m, SystemMessage)]
    assert _contents(saved_systems) == ["sys v1", "sys v2"]
