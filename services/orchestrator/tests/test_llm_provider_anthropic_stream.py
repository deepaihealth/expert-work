import pytest
from langchain_core.messages import HumanMessage

from orchestrator.llm.providers._streaming import AnthropicStreamAssembler
from orchestrator.llm.providers.anthropic import AnthropicProvider, RecordingAnthropicClient


def _text_events() -> list[dict]:
    return [
        {"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 3}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]


@pytest.mark.asyncio
async def test_stream_yields_deltas() -> None:
    client = RecordingAnthropicClient(stream_events=_text_events())
    provider = AnthropicProvider(client=client, model="claude-x", max_tokens=1024)
    out = [d async for d in provider.stream(messages=[HumanMessage(content="hi")], tools=[])]
    assert "".join(d.content for d in out if d.content) == "Hello"
    assert client.calls[-1]["stream"] is True


@pytest.mark.asyncio
async def test_stream_then_assemble_equals_complete() -> None:
    whole = {
        "content": [{"type": "text", "text": "Hello"}],
        "model": "claude-x",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    complete_p = AnthropicProvider(
        client=RecordingAnthropicClient(response=whole), model="claude-x", max_tokens=1024
    )
    expected = await complete_p.complete(messages=[HumanMessage(content="hi")], tools=[])

    stream_p = AnthropicProvider(
        client=RecordingAnthropicClient(stream_events=_text_events()),
        model="claude-x",
        max_tokens=1024,
    )
    asm = stream_p.new_stream_assembler()
    assert isinstance(asm, AnthropicStreamAssembler)
    async for d in stream_p.stream(messages=[HumanMessage(content="hi")], tools=[]):
        asm.add(d)
    got = asm.build()
    assert got.content == expected.content
    assert got.usage_metadata == expected.usage_metadata
    assert got.tool_calls == expected.tool_calls


def _tool_use_events() -> list[dict]:
    return [
        {"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 4}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"q": '},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"hi"}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 6},
        },
        {"type": "message_stop"},
    ]


@pytest.mark.asyncio
async def test_stream_then_assemble_tool_use_equals_complete() -> None:
    # Highest-complexity path: a tool_use SSE sequence assembled via stream()
    # must byte-equal complete()'s decode of the equivalent whole tool_use body.
    whole = {
        "model": "claude-x",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "hi"}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 4, "output_tokens": 6},
    }
    complete_p = AnthropicProvider(
        client=RecordingAnthropicClient(response=whole), model="claude-x", max_tokens=1024
    )
    expected = await complete_p.complete(messages=[HumanMessage(content="hi")], tools=[])

    stream_p = AnthropicProvider(
        client=RecordingAnthropicClient(stream_events=_tool_use_events()),
        model="claude-x",
        max_tokens=1024,
    )
    asm = stream_p.new_stream_assembler()
    async for d in stream_p.stream(messages=[HumanMessage(content="hi")], tools=[]):
        asm.add(d)
    got = asm.build()
    assert got.tool_calls == expected.tool_calls
    assert got.content == expected.content
    assert got.usage_metadata == expected.usage_metadata


@pytest.mark.asyncio
async def test_assembled_message_carries_the_stream_stop_reason() -> None:
    # B-105 —— 截断判定读 response_metadata["stop_reason"];流式与非流式同形。
    events = _text_events()
    events[3] = {
        "type": "message_delta",
        "delta": {"stop_reason": "max_tokens"},
        "usage": {"output_tokens": 2},
    }
    provider = AnthropicProvider(
        client=RecordingAnthropicClient(stream_events=events), model="claude-x", max_tokens=1024
    )
    asm = provider.new_stream_assembler()
    async for d in provider.stream(messages=[HumanMessage(content="hi")], tools=[]):
        asm.add(d)

    assert asm.build().response_metadata["stop_reason"] == "max_tokens"
