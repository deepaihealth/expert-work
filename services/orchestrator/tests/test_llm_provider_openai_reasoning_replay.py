"""B-195 —— DeepSeek 思考模式回传历史 ``reasoning_content``。

DeepSeek 官方文档(Thinking Mode):请求带 ``tools`` 时,之前每一轮 assistant 消息的
``reasoning_content`` 都要原样回传并拼进上下文;不带 ``tools`` 时不用回传。平台只给
DeepSeek 打开(``replay_reasoning_content=True``),其它厂商请求体不变。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from orchestrator.llm import RecordingOpenAIClient, make_deepseek_client
from orchestrator.llm.providers._streaming import OpenAIStreamAssembler
from orchestrator.llm.providers.openai_compatible import OpenAICompatibleProvider
from orchestrator.tools.registry import ToolSpec

_TOOLS = [ToolSpec(name="get_weather", description="weather of a city")]
_OK = {"choices": [{"message": {"content": "ok"}}]}


def _history() -> list[BaseMessage]:
    """两轮带工具的对话:第 1 轮先调工具再给答案,第 2 轮用户追问。"""
    return [
        HumanMessage(content="杭州明天天气?"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "get_weather", "args": {"city": "杭州"}}],
            additional_kwargs={"reasoning_content": "先查杭州天气"},
        ),
        ToolMessage(content="多云 7~13°C", tool_call_id="call_1"),
        AIMessage(
            content="杭州明天多云。",
            additional_kwargs={"reasoning_content": "工具结果够了,直接回答"},
        ),
        HumanMessage(content="那广州呢?"),
    ]


def _deepseek(client: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        client=client, model="deepseek-v4-pro", replay_reasoning_content=True
    )


def _assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m["role"] == "assistant"]


@pytest.mark.asyncio
async def test_deepseek_with_tools_replays_reasoning_on_tool_call_message() -> None:
    client = RecordingOpenAIClient(response=_OK)
    await _deepseek(client).complete(messages=_history(), tools=_TOOLS)
    assert _assistant(client.calls[0]["messages"])[0] == {
        "role": "assistant",
        "content": None,
        "reasoning_content": "先查杭州天气",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"city": "杭州"})},
            }
        ],
    }


@pytest.mark.asyncio
async def test_deepseek_with_tools_replays_reasoning_on_text_only_message() -> None:
    # 文档:没调工具的那一轮也要回传。
    client = RecordingOpenAIClient(response=_OK)
    await _deepseek(client).complete(messages=_history(), tools=_TOOLS)
    assert _assistant(client.calls[0]["messages"])[1] == {
        "role": "assistant",
        "content": "杭州明天多云。",
        "reasoning_content": "工具结果够了,直接回答",
    }


@pytest.mark.asyncio
async def test_deepseek_without_tools_sends_no_reasoning() -> None:
    client = RecordingOpenAIClient(response=_OK)
    await _deepseek(client).complete(messages=_history(), tools=[])
    assert all("reasoning_content" not in m for m in client.calls[0]["messages"])


@pytest.mark.asyncio
async def test_assistant_message_without_reasoning_gets_no_key() -> None:
    # 不凭空造空串:没有思考内容(或是空串)的 assistant 消息照旧。
    history: list[BaseMessage] = [
        HumanMessage(content="hi"),
        AIMessage(content="hello"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "get_weather", "args": {}}],
            additional_kwargs={"reasoning_content": ""},
        ),
        ToolMessage(content="晴", tool_call_id="call_1"),
    ]
    client = RecordingOpenAIClient(response=_OK)
    await _deepseek(client).complete(messages=history, tools=_TOOLS)
    assert all("reasoning_content" not in m for m in client.calls[0]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["glm-5.2", "kimi-k3", "qwen3.7-max"])
async def test_compat_provider_without_flag_never_replays(model: str) -> None:
    client = RecordingOpenAIClient(response=_OK)
    provider = OpenAICompatibleProvider(client=client, model=model)
    await provider.complete(messages=_history(), tools=_TOOLS)
    assert all("reasoning_content" not in m for m in client.calls[0]["messages"])


@pytest.mark.asyncio
async def test_stream_and_complete_put_the_same_messages_on_the_wire() -> None:
    complete_client = RecordingOpenAIClient(response=_OK)
    await _deepseek(complete_client).complete(messages=_history(), tools=_TOOLS)

    stream_client = RecordingOpenAIClient(response=_OK)
    async for _ in _deepseek(stream_client).stream(messages=_history(), tools=_TOOLS):
        pass

    assert stream_client.calls[0]["stream"] is True
    assert stream_client.calls[0]["messages"] == complete_client.calls[0]["messages"]
    assert [m.get("reasoning_content") for m in _assistant(stream_client.calls[0]["messages"])] == [
        "先查杭州天气",
        "工具结果够了,直接回答",
    ]


@pytest.mark.asyncio
async def test_streamed_reasoning_round_trips_into_the_next_request() -> None:
    # 解码侧(流式拼装 → additional_kwargs)与回传侧用的是同一个键。
    first = RecordingOpenAIClient(
        stream_chunks=[
            {"choices": [{"delta": {"reasoning_content": "先查"}}]},
            {"choices": [{"delta": {"reasoning_content": "天气"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_9",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
    )
    provider = _deepseek(first)
    assembler = OpenAIStreamAssembler()
    async for delta in provider.stream(messages=[HumanMessage(content="天气?")], tools=_TOOLS):
        assembler.add(delta)
    reply = assembler.build()

    second = RecordingOpenAIClient(response=_OK)
    provider.client = second
    await provider.complete(
        messages=[
            HumanMessage(content="天气?"),
            reply,
            ToolMessage(content="晴", tool_call_id="call_9"),
        ],
        tools=_TOOLS,
    )
    assert _assistant(second.calls[0]["messages"])[0]["reasoning_content"] == "先查天气"


@pytest.mark.asyncio
async def test_reasoning_reaches_the_http_body() -> None:
    bodies: list[dict[str, Any]] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_OK)

    client = make_deepseek_client("sk-test", transport=httpx.MockTransport(_handler))
    await _deepseek(client).complete(messages=_history(), tools=_TOOLS)
    assert [m.get("reasoning_content") for m in _assistant(bodies[0]["messages"])] == [
        "先查杭州天气",
        "工具结果够了,直接回答",
    ]
