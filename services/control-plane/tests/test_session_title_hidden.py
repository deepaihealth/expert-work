"""B-67 —— 会话标题回填不能取隐藏的「本轮输入」段。

``input`` 可选:只带 ``inputs`` 的 jinja run,用户消息正文为空,``first_message_title``
会跳过它去看下一条 human —— 那条正是平台生成的隐藏段。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from control_plane.api._session_title import first_message_title
from control_plane.api.runs import build_run_graph_input
from expert_work.protocol import PromptVariableSpec


class _StubCheckpointer:
    def __init__(self, messages: list[BaseMessage]) -> None:
        self._messages = messages

    async def aget_tuple(self, config: Any) -> Any:
        del config
        return SimpleNamespace(checkpoint={"channel_values": {"messages": self._messages}})


def _jinja_turn(input_text: str | None) -> list[BaseMessage]:
    built = SimpleNamespace(
        supports_vision=False,
        spotlight_nonce=None,
        max_steps=5,
        max_no_progress=0,
        system_prompt="unused",
        prompt_jinja=True,
        prompt_base="hi {{ who }}",
        prompt_suffix="",
        prompt_variables=(PromptVariableSpec(name="who"),),
        arg_bindings=(),
    )
    gi = build_run_graph_input(
        built,
        input_text=input_text,
        image_refs=[],
        untrusted_content=None,
        inputs={"who": "x"},
        run_id=uuid4(),
    )
    return list(gi["messages"])


@pytest.mark.asyncio
async def test_title_never_comes_from_the_hidden_inputs_block() -> None:
    turn = _jinja_turn(None)
    assert turn[1].content == ""  # 用户消息正文为空 —— 前提成立
    cp = _StubCheckpointer([*turn, AIMessage(content="reply")])
    assert await first_message_title(cp, uuid4()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_title_skips_the_block_and_takes_a_later_user_message() -> None:
    cp = _StubCheckpointer(
        [
            *_jinja_turn(None),
            AIMessage(content="reply"),
            *_jinja_turn("second question"),
        ]
    )
    assert await first_message_title(cp, uuid4()) == "second question"  # type: ignore[arg-type]
