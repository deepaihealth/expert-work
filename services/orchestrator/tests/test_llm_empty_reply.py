"""B-191 —— 正常结束却既没有回答也没有工具调用:判定。主循环里的重试 / 失败(含报错文案)见
``test_react_graph.py`` 的 B-191 一节。"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from orchestrator.llm.empty_reply import is_empty_reply, is_filtered
from orchestrator.llm.stall import STALLED_AFTER_S

_STOP = {"finish_reason": "stop"}
_CALL = {"name": "write_file", "args": {}, "id": "call_1", "type": "tool_call"}


def test_normal_stop_without_text_or_tool_calls_is_empty() -> None:
    # 实测形态:工具调用的标记漏进了思考,正文与 tool_calls 都是空的。判定不看思考内容。
    leaked = AIMessage(
        content="",
        response_metadata=_STOP,
        additional_kwargs={"reasoning_content": "...</parameter></invoke></tool_calls>"},
    )
    assert is_empty_reply(leaked)
    assert is_empty_reply(AIMessage(content=" \n", response_metadata=_STOP))
    # 没带结束原因的厂商 / 网关,空着也算。
    assert is_empty_reply(AIMessage(content=""))


def test_thinking_blocks_and_blank_text_are_not_content() -> None:
    thinking_only = [
        {"type": "thinking", "thinking": "..."},
        {"type": "redacted_thinking", "data": "..."},
        {"type": "text", "text": "  \n"},
    ]
    assert is_empty_reply(AIMessage(content=thinking_only, response_metadata=_STOP))  # type: ignore[arg-type]


def test_any_other_block_counts_as_content() -> None:
    image_only = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    assert not is_empty_reply(AIMessage(content=image_only, response_metadata=_STOP))  # type: ignore[arg-type]
    with_text = [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "答案"}]
    assert not is_empty_reply(AIMessage(content=with_text, response_metadata=_STOP))  # type: ignore[arg-type]


def test_text_or_a_tool_call_is_not_empty() -> None:
    assert not is_empty_reply(AIMessage(content="答案", response_metadata=_STOP))
    assert not is_empty_reply(AIMessage(content="", tool_calls=[_CALL], response_metadata=_STOP))


def test_only_unparseable_tool_calls_is_empty() -> None:
    # 图只派发 ``tool_calls``;只带解析失败的调用会被当成说完了收尾 —— 同样是空着的成功。
    invalid = {"name": "write_file", "args": "{", "id": "call_2", "error": "bad json"}
    assert is_empty_reply(
        AIMessage(content="", invalid_tool_calls=[invalid], response_metadata=_STOP)
    )


def test_truncation_and_stall_belong_to_their_own_checks() -> None:
    # B-105 / B-158 管截断,B-186 管停顿:都不在这里重复判。
    assert not is_empty_reply(AIMessage(content="", response_metadata={"finish_reason": "length"}))
    assert not is_empty_reply(
        AIMessage(content="", response_metadata={"stop_reason": "max_tokens"})
    )
    assert not is_empty_reply(AIMessage(content="", response_metadata={STALLED_AFTER_S: 45.0}))


def test_vendor_content_filter_stops_are_recognised() -> None:
    for meta in (
        {"finish_reason": "content_filter"},
        {"finish_reason": "sensitive"},
        {"stop_reason": "refusal"},
    ):
        assert is_filtered(AIMessage(content="", response_metadata=meta)), meta
    assert not is_filtered(AIMessage(content="", response_metadata=_STOP))
    assert not is_filtered(AIMessage(content="", response_metadata={"stop_reason": "end_turn"}))
