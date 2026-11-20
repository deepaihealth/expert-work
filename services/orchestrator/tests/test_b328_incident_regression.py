"""B-328 §6.1 —— 复现测试环境 2026-11-20 那次卡死的形状(``agent_node`` 级)。

一个智能体把压缩门槛配成 6 万(窗口 100 万),第 2 轮 ``read_document`` 一次返回 44,785
token,落在「最后 6 条永不总结」的尾巴里。修之前:第 2 轮第 3 步、第 3 轮普通追问、第 3 轮
「重新生成」三个场景都按 ``ContextOverflowError`` 失败(60301 / 60379)。

假估算器按 spec §1.1 的表给每条消息定 token 数(按消息里的标记认),其余文字 1 字 = 1
token —— 这样 ``read_document`` 那条截短之后的大小也是按真实长度量的。真实的三道闸
(清理 4 万 / 最近 4 条、滑窗 4 万、压缩 6 万 + 头 4 尾 6)、真实的 per_session 记忆注入。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from prometheus_client import REGISTRY

from expert_work.common.conversation_channel import (
    HIDE_FROM_UI,
    INPUTS_BLOCK_MARK,
    SUPERSEDED_BY,
)
from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.common.spotlight import spotlight_untrusted
from expert_work.protocol import MemoryItem
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.context import ContextCompressor, ToolResultPruner, WorkingWindow
from orchestrator.context.compressor import estimate_tokens
from orchestrator.context.result_budget import context_budget_for

NONCE = "b328nonce"
WINDOW = 1_000_000

#: spec §1.1 —— 标记 → 平台估算 token 数。
_TABLE: dict[str, int] = {
    "⟦sys⟧": 12_045,
    "⟦mem⟧": 527,
    "⟦u1⟧": 47,
    "⟦i1⟧": 307,
    "⟦ks1⟧": 1_692,
    "⟦ks2⟧": 322,
    "⟦ans1⟧": 146,
    "⟦u2⟧": 38,
    "⟦i2⟧": 315,
    "⟦ex⟧": 1_162,
    "⟦u3⟧": 47,
    "⟦i3⟧": 315,
    # 摘要:第 2 轮第 2 步产出的(缓存在检查点里)、第 3 步更新出的、第 3 轮更新出的。
    "⟦S757⟧": 757,
    "⟦S1072⟧": 1_072,
    "⟦S1141⟧": 1_141,
}


class TableEstimator:
    """按标记给数;没有标记的文字 1 字 = 1 token(空串 1)。"""

    def count(self, text: str) -> int:
        for marker, tokens in _TABLE.items():
            if marker in text:
                return tokens
        return max(1, len(text))


ESTIMATOR = TableEstimator()


def _doc_result_content() -> str:
    """``read_document`` 结果:围栏包住的正文,整条恰好 44,785「token」(1 字 1 token)。"""
    overhead = len(spotlight_untrusted("", nonce=NONCE))
    body = "文" * (44_785 - overhead)
    content = spotlight_untrusted(body, nonce=NONCE)
    assert len(content) == 44_785
    return content


@dataclass
class _Summariser:
    """按调用次序返回带标记的摘要正文。"""

    bodies: list[str]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        return AIMessage(content=body)


@dataclass
class _RecordingLLM:
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        return AIMessage(content="好的")


def _memory() -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content="⟦mem⟧ 用户偏好简体中文",
        embedding=(0.0,),
    )


def _stamp(run: str) -> dict[str, Any]:
    return {STAMP_RUN_ID: run}


def _inputs(marker: str, run: str) -> HumanMessage:
    return HumanMessage(
        content=f"[本轮输入] {marker}",
        additional_kwargs={HIDE_FROM_UI: True, INPUTS_BLOCK_MARK: True, **_stamp(run)},
    )


def _turn1_and_2() -> list[BaseMessage]:
    return [
        SystemMessage(content="⟦sys⟧ 你是健康管理助手"),
        HumanMessage(content="⟦u1⟧ 第一轮问题", additional_kwargs=_stamp("r1")),
        _inputs("⟦i1⟧", "r1"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "knowledge_search", "args": {"query": "q"}, "id": "c-ks"},
                {"name": "mcp:docs.fetch", "args": {"id": "x"}, "id": "c-mcp"},
            ],
            additional_kwargs=_stamp("r1"),
        ),
        ToolMessage(content="⟦ks1⟧ 资料", tool_call_id="c-ks", name="knowledge_search", id="t-ks1"),
        ToolMessage(content="⟦ks2⟧ 资料", tool_call_id="c-mcp", name="mcp:docs.fetch", id="t-ks2"),
        AIMessage(content="⟦ans1⟧ 第一轮回答", additional_kwargs=_stamp("r1")),
        HumanMessage(content="⟦u2⟧ 帮我读这份文档", additional_kwargs=_stamp("r2")),
        _inputs("⟦i2⟧", "r2"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "read_document", "args": {"path": "uploads/报告.pdf"}, "id": "c-rd"}
            ],
            additional_kwargs=_stamp("r2"),
        ),
        ToolMessage(
            content=_doc_result_content(),
            tool_call_id="c-rd",
            name="read_document",
            id="t-rd",
            artifact={
                "chars": 50_792,
                "figures": 0,
                "figures_state": "none",
                "format": "pdf",
                "path": "uploads/报告.pdf",
                "truncated": False,
            },
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "exec_python", "args": {"code": "print(1)"}, "id": "c-ex"}],
            additional_kwargs=_stamp("r2"),
        ),
        ToolMessage(content="⟦ex⟧ 1", tool_call_id="c-ex", name="exec_python", id="t-ex"),
    ]


def _turn3(run: str) -> list[BaseMessage]:
    return [
        HumanMessage(content="⟦u3⟧ 然后呢", additional_kwargs=_stamp(run)),
        _inputs("⟦i3⟧", run),
    ]


def _superseded(messages: list[BaseMessage], by: str) -> list[BaseMessage]:
    return [
        m.model_copy(update={"additional_kwargs": {**m.additional_kwargs, SUPERSEDED_BY: by}})
        for m in messages
    ]


def _cached_summary() -> dict[str, str]:
    """第 2 轮第 2 步写回检查点的那份摘要(覆盖到第一条知识检索结果)。"""
    return {
        "through_id": "t-ks1",
        "text": "<context-summary>\n⟦S757⟧ 早先的摘要\n</context-summary>",
    }


def _scenario(name: str) -> tuple[list[BaseMessage], list[str]]:
    """返回 (检查点里的消息, 摘要器依次返回的正文)。"""
    base = _turn1_and_2()
    if name == "turn2_step3":
        return base, ["⟦S1072⟧ 更新后的摘要"]
    if name == "turn3_followup":
        return [*base, *_turn3("r3")], ["⟦S1141⟧ 更新后的摘要"]
    # 第 3 轮「重新生成」:旧的第 3 轮整段被取代,新的第 3 轮同样的用户消息。
    old = _superseded(_turn3("r3"), by="r3b")
    return [*base, *old, *_turn3("r3b")], ["⟦S1141⟧ 更新后的摘要"]


def _graph(llm: _RecordingLLM, summariser: _Summariser, *, budget: bool) -> Any:
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=WINDOW,
        threshold_pct=0.7,
        head_keep=4,
        tail_keep=6,
        estimator=ESTIMATOR,
        absolute_cap_tokens=60_000,
    )
    window = WorkingWindow(context_window=WINDOW, estimator=ESTIMATOR, absolute_cap_tokens=40_000)
    pruner = ToolResultPruner(
        context_window=WINDOW,
        recent_tool_results_kept=4,
        estimator=ESTIMATOR,
        absolute_cap_tokens=40_000,
    )
    return build_react_graph(
        llm_caller=llm,
        tool_registry=ToolRegistry(),
        context_compressor=compressor,
        working_window=window,
        tool_result_pruner=pruner,
        context_budget=(
            context_budget_for(
                window_tokens=WINDOW,
                threshold_pct=0.7,
                absolute_cap_tokens=60_000,
                estimator=ESTIMATOR,
            )
            if budget
            else None
        ),
        token_estimator=ESTIMATOR,
        spotlight_nonce=NONCE,
        memory_recall_mode="per_session",
    )


async def _run(
    name: str, *, budget: bool = True
) -> tuple[_RecordingLLM, list[BaseMessage], list[BaseMessage]]:
    history, bodies = _scenario(name)
    llm = _RecordingLLM()
    async with make_checkpointer("memory") as cp:
        graph = _graph(llm, _Summariser(bodies), budget=budget)
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": history,
                "recalled_memories": [_memory()],
                "context_summary": _cached_summary(),
                "step_count": 0,
                "max_steps": 20,
            },
            config=cfg,
        )
        snapshot = await compiled.aget_state(cfg)
    return llm, history, list(snapshot.values["messages"])


#: 修之前(``main`` 630dcf9fd)这三个场景以 ``context overflow: estimated <N> tokens >
#: threshold 60000 after 1 compression pass(es)`` 失败,N 就是下面的数(开发时先跑红、逐字
#: 对上过);修之后压缩器压到同一个数、交回照发。
SCENARIOS = ["turn2_step3", "turn3_followup", "turn3_regenerate"]
ESTIMATE_AFTER_COMPRESSION = {
    "turn2_step3": 60_301,
    "turn3_followup": 60_379,
    "turn3_regenerate": 60_379,
}


def _doc_message(prompt: Sequence[BaseMessage]) -> ToolMessage:
    found = [m for m in prompt if isinstance(m, ToolMessage) and m.name == "read_document"]
    assert len(found) == 1
    return found[0]


def _over_threshold() -> float:
    value = REGISTRY.get_sample_value(
        "expert_work_cm_compressor_over_threshold_total", {"cause": "nothing_to_summarise"}
    )
    return value or 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIOS)
async def test_incident_shape_no_longer_fails(name: str) -> None:
    llm, history, checkpoint = await _run(name)

    assert len(llm.prompts) == 1  # 发出去了,模型答完这一轮
    doc = _doc_message(llm.prompts[0])
    content = str(doc.content)
    # A② —— 截到单条预算 R = 60,000 / 5 以内。
    assert estimate_tokens([doc], estimator=ESTIMATOR) <= 12_000
    # 围栏:同一个 nonce,只有一对;说明在关围栏之后。
    open_marker, close_marker = f"«UNTRUSTED nonce={NONCE}»", f"«/UNTRUSTED nonce={NONCE}»"
    assert content.startswith(open_marker + "\n")
    assert content.count(open_marker) == 1
    assert content.count(close_marker) == 1
    note_at = content.find("call read_document again on the original file uploads/报告.pdf")
    assert note_at > content.find(close_marker)
    # 检查点逐字不变(只改发给模型的那一份)。
    assert [(type(m), m.content) for m in checkpoint[: len(history)]] == [
        (type(m), m.content) for m in history
    ]
    if name == "turn3_regenerate":
        # 取代过滤只剔除了旧的第 3 轮:用户消息只剩新的那一条,文档结果还在。
        users = [m for m in llm.prompts[0] if "⟦u3⟧" in str(m.content)]
        assert len(users) == 1


@pytest.mark.asyncio
async def test_incident_prompt_is_identical_across_runs() -> None:
    first, _, _ = await _run("turn3_followup")
    second, _, _ = await _run("turn3_followup")
    assert [m.content for m in first.prompts[0]] == [m.content for m in second.prompts[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIOS)
async def test_incident_shape_without_shrink_is_sent_over_threshold(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A① 单独也解开:不截大结果时压缩器压到的还是修之前报错里那个数,交回照发。"""
    before = _over_threshold()
    with caplog.at_level(logging.WARNING, logger="orchestrator.context.compressor"):
        llm, _, _ = await _run(name, budget=False)
    assert len(llm.prompts) == 1
    assert _over_threshold() == before + 1
    expected = f"estimated_tokens={ESTIMATE_AFTER_COMPRESSION[name]} threshold=60000"
    assert any(expected in r.getMessage() for r in caplog.records)


def test_estimator_reproduces_spec_numbers() -> None:
    """自检:表里的数加起来就是 spec §1.2 第 2 步的 60,226(还没有 exec_python 那两条)。"""
    msgs = _turn1_and_2()[:-2]
    memory_tokens = 527
    assert estimate_tokens(msgs, estimator=ESTIMATOR) + memory_tokens == 60_226
