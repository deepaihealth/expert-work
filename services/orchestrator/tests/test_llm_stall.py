"""B-186 —— 思考完停住、一个字的回答都没有:判定、报错文案,以及规划步骤仍然 fail-open。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from orchestrator.graph_builder.planner import make_planner_node
from orchestrator.llm.providers._streaming import LLMDelta, OpenAIStreamAssembler
from orchestrator.llm.router import LLMRouter, ProviderHandle
from orchestrator.llm.stall import (
    STALLED_AFTER_S,
    StalledWithoutAnswerError,
    stalled_without_answer,
)

_STAMP = {STALLED_AFTER_S: 45.0}


def test_stamped_reply_without_text_or_tool_calls_is_stalled() -> None:
    assert stalled_without_answer(AIMessage(content="", response_metadata=_STAMP)) == 45.0
    assert stalled_without_answer(AIMessage(content=" \n", response_metadata=_STAMP)) == 45.0


def test_stamped_reply_with_answer_text_or_a_tool_call_is_not_stalled() -> None:
    assert stalled_without_answer(AIMessage(content="半句回答", response_metadata=_STAMP)) is None
    call = {"name": "search", "args": {}, "id": "call_1", "type": "tool_call"}
    assert (
        stalled_without_answer(AIMessage(content="", tool_calls=[call], response_metadata=_STAMP))
        is None
    )


def test_unstamped_empty_reply_is_not_stalled() -> None:
    # 没盖章 = 不是停顿超时(正常结束的空回复、或厂商已发结束原因),不归这里管。
    assert stalled_without_answer(AIMessage(content="")) is None


def test_message_names_the_pause_and_the_setting_but_no_model_or_key() -> None:
    text = str(StalledWithoutAnswerError(45.0))
    assert "停顿超过 45 秒" in text
    assert "token 间隔超时" in text
    assert "deepseek" not in text and "#" not in text


class _ThinksThenStalls:
    async def stream(
        self, *, messages: Any, tools: Any, output_schema: Any = None
    ) -> AsyncIterator[LLMDelta]:
        del messages, tools, output_schema
        yield LLMDelta(reasoning="让我想想")
        await asyncio.sleep(0.3)

    def new_stream_assembler(self) -> OpenAIStreamAssembler:
        return OpenAIStreamAssembler()


@pytest.mark.asyncio
async def test_planner_still_falls_back_to_a_single_step_plan_on_a_stall() -> None:
    # 规划把坏回复降级成单步计划,run 照常往下走(Mini-ADR J-3a)。停顿判失败只在主循环,
    # 不能经由共用的路由连累到这里。
    router = LLMRouter(
        providers=[ProviderHandle(provider=_ThinksThenStalls(), key="deepseek:deepseek-flash")],  # type: ignore[arg-type]
        first_token_timeout_s=0.5,
        idle_timeout_s=0.1,
    )
    node = make_planner_node(router)

    update = await node({"messages": [HumanMessage(content="do X")]}, {})  # type: ignore[typeddict-item]

    assert [step.description for step in update["plan"].steps] == ["do X"]
