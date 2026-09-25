"""B-105 —— 输出被上限截断的判定。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from orchestrator.llm.truncation import OutputTruncatedError, is_truncated, is_unusable_truncation


def _msg(
    content: str | list[Any] = "",
    *,
    finish: str | None = None,
    stop: str | None = None,
    tools: bool = False,
) -> AIMessage:
    meta: dict[str, str] = {}
    if finish:
        meta["finish_reason"] = finish
    if stop:
        meta["stop_reason"] = stop
    calls = [{"id": "c1", "name": "write_file", "args": {}, "type": "tool_call"}] if tools else []
    return AIMessage(content=content, tool_calls=calls, response_metadata=meta)


def test_openai_length_and_anthropic_max_tokens_are_truncation() -> None:
    assert is_truncated(_msg("x", finish="length"))
    assert is_truncated(_msg("x", stop="max_tokens"))
    assert not is_truncated(_msg("x", finish="stop"))
    assert not is_truncated(_msg("x", stop="end_turn"))
    assert not is_truncated(_msg("x"))


def test_empty_text_after_truncation_is_unusable() -> None:
    assert is_unusable_truncation(_msg("", finish="length"))
    assert is_unusable_truncation(_msg("   ", finish="length"))


def test_truncated_with_tool_calls_is_unusable() -> None:
    # 最后一个工具调用的参数必然被截,_parse_arguments 会静默给 {}。
    assert is_unusable_truncation(_msg("好的,我来写文件", finish="length", tools=True))


def test_truncated_text_only_is_usable() -> None:
    assert not is_unusable_truncation(_msg("一段完整度够用的回答", finish="length"))
    assert not is_unusable_truncation(_msg("", finish="stop"))


def test_block_list_content_counts_only_text_blocks() -> None:
    # 只有思考块、没有 text 块 = 正文为空;有 text 块 = 可用。
    thinking_only = [{"type": "thinking", "thinking": "想了很久"}]
    assert is_unusable_truncation(_msg(thinking_only, stop="max_tokens"))
    with_text = [{"type": "text", "text": "答案"}]
    assert not is_unusable_truncation(_msg(with_text, stop="max_tokens"))


def test_error_message_names_the_cap_or_vendor_default() -> None:
    err = OutputTruncatedError(2048)
    assert err.cap == 2048
    assert "模型输出被截断" in str(err) and "2048" in str(err)
    assert "厂商默认值" in str(OutputTruncatedError(None))


def test_served_cap_reports_the_4096_anthropic_actually_sends() -> None:
    """Anthropic 清单没设上限时请求里发的是 4096;截断标签 / 报错里的上限要是这个数,
    不能说成「厂商默认值」。其他厂商为空照旧是 ``None``。"""
    from expert_work.protocol import ModelSpec
    from orchestrator.llm.truncation import served_output_cap

    resolve = served_output_cap(
        ModelSpec.model_validate(
            {
                "provider": "anthropic",
                "name": "claude-sonnet-4-6",
                "fallback": [
                    {"provider": "glm", "name": "glm-5.3"},
                    {"provider": "anthropic", "name": "claude-opus-4-8", "max_tokens": 8000},
                ],
            }
        )
    )
    assert resolve(AIMessage(content="")) == ("anthropic", "claude-sonnet-4-6", 4096)
    assert resolve.caps[("glm", "glm-5.3")] is None
    assert resolve.caps[("anthropic", "claude-opus-4-8")] == 8000
