"""Unit tests for :class:`OpenAIProvider` + :class:`HTTPOpenAIClient`
— Stream E.11.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from expert_work.runtime.middleware import (
    LLMClientError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
)
from orchestrator.llm import (
    HTTPOpenAIClient,
    OpenAIProvider,
    RecordingOpenAIClient,
)
from orchestrator.multimodal import (
    DispatchingImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ResolvedImage,
    image_ref_block,
)
from orchestrator.tools.registry import ToolSpec

# ---------------------------------------------------------------------------
# Recording client — request shape mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_message_inline_in_messages_array() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"content": "ok"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    await provider.complete(
        messages=[SystemMessage(content="be helpful"), HumanMessage(content="hi")],
        tools=[],
    )

    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.asyncio
async def test_ai_message_with_tool_calls_emits_function_call_format() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"content": "ok"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    history_ai = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "name": "read_file",
                "args": {"path": "/etc/hosts"},
                "type": "tool_call",
            }
        ],
    )

    await provider.complete(
        messages=[
            HumanMessage(content="read hosts"),
            history_ai,
            ToolMessage(content="127.0.0.1", tool_call_id="call_1"),
        ],
        tools=[],
    )

    sent = client.calls[0]["messages"]
    assert sent[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/etc/hosts"}),
                },
            }
        ],
    }
    assert sent[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "127.0.0.1",
    }


@pytest.mark.asyncio
async def test_tool_spec_emits_function_schema() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"content": "ok"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")
    spec = ToolSpec(
        name="search",
        description="search the web",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[spec])

    assert client.calls[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "search the web",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_empty_tools_yields_no_tools_field() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"content": "ok"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["tools"] is None


@pytest.mark.asyncio
async def test_temperature_passed_to_client_when_set() -> None:
    client = RecordingOpenAIClient(response={"choices": [{"message": {"content": "ok"}}]})
    provider = OpenAIProvider(client=client, model="gpt-4o-mini", temperature=0.7)

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["temperature"] == 0.7


@pytest.mark.asyncio
async def test_temperature_defaults_to_none() -> None:
    """No temperature → None reaches the client → omitted from the body."""
    client = RecordingOpenAIClient(response={"choices": [{"message": {"content": "ok"}}]})
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["temperature"] is None


# ---------------------------------------------------------------------------
# Response decoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_only_choice_yields_plain_ai_message() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"role": "assistant", "content": "hello"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.content == "hello"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_usage_metadata_extracted_for_metering() -> None:
    # Event-stream + metering — body.usage maps to AIMessage.usage_metadata
    # (TokenUsageMiddleware reads it; without it compat vendors meter zero).
    client = RecordingOpenAIClient(
        response={
            "model": "doubao-seed-2-1-pro-260628",
            "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 64},
                "completion_tokens_details": {"reasoning_tokens": 18},
            },
        },
    )
    provider = OpenAIProvider(client=client, model="doubao-seed-2-1-pro-260628")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.usage_metadata == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "input_token_details": {"cache_read": 64},
        "output_token_details": {"reasoning": 18},
    }
    assert result.response_metadata == {
        "finish_reason": "stop",
        "model_name": "doubao-seed-2-1-pro-260628",
    }


@pytest.mark.asyncio
async def test_reasoning_content_surfaced_in_additional_kwargs() -> None:
    client = RecordingOpenAIClient(
        response={
            "choices": [{"message": {"content": "answer", "reasoning_content": "let me think..."}}],
        },
    )
    provider = OpenAIProvider(client=client, model="qwen3.7-max")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.content == "answer"
    assert result.additional_kwargs == {"reasoning_content": "let me think..."}


@pytest.mark.asyncio
async def test_no_usage_yields_none_metadata() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"content": "hi"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.usage_metadata is None
    assert result.additional_kwargs == {}
    assert result.response_metadata == {}


@pytest.mark.asyncio
async def test_tool_calls_decoded_with_json_args() -> None:
    client = RecordingOpenAIClient(
        response={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"q": "hello"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.content == ""
    assert result.tool_calls == [
        {"id": "call_abc", "name": "search", "args": {"q": "hello"}, "type": "tool_call"},
    ]


@pytest.mark.asyncio
async def test_malformed_arguments_string_decodes_to_empty_dict() -> None:
    """OpenAI sometimes streams partial JSON in ``arguments``; we tolerate
    rather than raise, matching the design choice in
    :func:`orchestrator.llm.providers.openai._parse_arguments`."""
    client = RecordingOpenAIClient(
        response={
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "x", "arguments": "{not json"},
                            }
                        ],
                    }
                }
            ]
        },
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.tool_calls[0]["args"] == {}


@pytest.mark.asyncio
async def test_arguments_already_dict_passes_through() -> None:
    client = RecordingOpenAIClient(
        response={
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "x", "arguments": {"a": 1}},
                            }
                        ],
                    }
                }
            ]
        },
    )
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.tool_calls[0]["args"] == {"a": 1}


@pytest.mark.asyncio
async def test_empty_choices_yields_empty_ai_message() -> None:
    client = RecordingOpenAIClient(response={"choices": []})
    provider = OpenAIProvider(client=client, model="gpt-4o-mini")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.content == ""
    assert result.tool_calls == []


# ---------------------------------------------------------------------------
# HTTPOpenAIClient — error mapping
# ---------------------------------------------------------------------------


def _http_client(handler) -> HTTPOpenAIClient:  # type: ignore[no-untyped-def]
    return HTTPOpenAIClient(
        api_key="sk-test",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )


async def _call(client: HTTPOpenAIClient) -> object:
    return await client.chat_completions(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
    )


@pytest.mark.asyncio
async def test_http_429_raises_rate_limit_error() -> None:
    client = _http_client(lambda _req: httpx.Response(429, text="rate_limit"))
    with pytest.raises(LLMRateLimitError, match="429"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_401_raises_client_error() -> None:
    client = _http_client(lambda _req: httpx.Response(401, text="unauthorized"))
    with pytest.raises(LLMClientError, match="401"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_502_raises_server_error() -> None:
    client = _http_client(lambda _req: httpx.Response(502, text="bad gateway"))
    with pytest.raises(LLMServerError, match="502"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_network_error_raises_network_error() -> None:
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    client = _http_client(_boom)
    with pytest.raises(LLMNetworkError, match="dns"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_200_returns_parsed_json() -> None:
    client = _http_client(
        lambda _req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    )
    body = await _call(client)
    assert body == {"choices": [{"message": {"content": "ok"}}]}


@pytest.mark.asyncio
async def test_http_temperature_included_in_request_body() -> None:
    captured: dict[str, object] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _http_client(_handler)
    await client.chat_completions(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        temperature=0.5,
    )
    assert captured["temperature"] == 0.5
    # ``None`` keeps it out of the body entirely.
    captured.clear()
    await client.chat_completions(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
    )
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_http_200_non_object_body_raises_server_error() -> None:
    client = _http_client(lambda _req: httpx.Response(200, json=["nope"]))
    with pytest.raises(LLMServerError, match="non-object"):
        await _call(client)


# ---------------------------------------------------------------------------
# Multimodal — image_ref content blocks (Stream J.6 Path A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_image_ref_emits_image_url_block() -> None:
    uri = "expert_work://image/demo.png"
    resolver = InMemoryImageResolver(
        images={uri: ResolvedImage(media_type="image/png", data=b"PNGBYTES")}
    )
    client = RecordingOpenAIClient(response={"choices": [{"message": {"content": "ok"}}]})
    provider = OpenAIProvider(client=client, model="gpt-4o", image_resolver=resolver)

    await provider.complete(
        messages=[
            HumanMessage(content=[{"type": "text", "text": "what is this?"}, image_ref_block(uri)])
        ],
        tools=[],
    )

    content = client.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_human_image_ref_dropped_without_resolver() -> None:
    uri = "expert_work://image/demo.png"
    client = RecordingOpenAIClient(response={"choices": [{"message": {"content": "ok"}}]})
    provider = OpenAIProvider(client=client, model="gpt-4o")  # no image_resolver

    await provider.complete(
        messages=[HumanMessage(content=[{"type": "text", "text": "hi"}, image_ref_block(uri)])],
        tools=[],
    )

    # Image silently dropped → plain-text content, no crash.
    assert client.calls[0]["messages"][0]["content"] == "hi"


@pytest.mark.asyncio
async def test_human_workspace_ref_resolves_through_dispatching_resolver(tmp_path: Path) -> None:
    """B-64 —— Path A 的适配器现在也认得工作区 ref。

    这条不是靠假设:``DispatchingImageResolver`` 把上传 ref 与工作区 ref 收进
    同一个实例后,provider 适配器一个字节都不用改就已经能解析新 scheme
    ——证明 Task 5 关的正是「Path A 的字节永远到不了模型」这个洞,不用等
    Path A 自己的任务落地。
    """
    tenant, user = uuid4(), uuid4()
    page_dir = tmp_path / str(tenant) / str(user) / "r1"
    page_dir.mkdir(parents=True)
    (page_dir / "page-01.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    uri = f"expert_work://workspace/{tenant}/{user}/r1/page-01.jpg"
    resolver = DispatchingImageResolver(
        uploads=InMemoryImageResolver({}), workspace=NasWorkspaceImageResolver(root=tmp_path)
    )
    client = RecordingOpenAIClient(response={"choices": [{"message": {"content": "ok"}}]})
    provider = OpenAIProvider(client=client, model="gpt-4o", image_resolver=resolver)

    await provider.complete(
        messages=[
            HumanMessage(content=[{"type": "text", "text": "what is this?"}, image_ref_block(uri)])
        ],
        tools=[],
    )

    content = client.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_http_extra_body_merges_into_request_body() -> None:
    """Stream CM-10 (Mini-ADR CM-L3) — vendor thinking fields on the wire."""
    captured: dict[str, object] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _http_client(_handler)
    await client.chat_completions(
        model="qwen3.7-max",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        extra_body={"enable_thinking": True, "thinking_budget": 8_000},
    )
    assert captured["enable_thinking"] is True
    assert captured["thinking_budget"] == 8_000


@pytest.mark.asyncio
async def test_http_no_extra_body_keeps_request_byte_identical() -> None:
    bodies: list[bytes] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        bodies.append(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _http_client(_handler)
    await _call(client)
    await client.chat_completions(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        extra_body=None,
    )
    assert bodies[0] == bodies[1]


@pytest.mark.asyncio
async def test_provider_threads_thinking_payload() -> None:
    recording = RecordingOpenAIClient(response={"choices": [{"message": {"content": "ok"}}]})
    provider = OpenAIProvider(
        client=recording,
        model="glm-5.1",
        thinking_payload={"thinking": {"type": "enabled"}},
    )
    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])
    assert recording.calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}


# --- Stream HX-13 — allowed_tools tier ---------------------------------------


@pytest.mark.asyncio
async def test_defer_markers_build_allowed_tools_subset() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-5.5")
    await provider.complete(
        messages=[HumanMessage(content="hi")],
        tools=[
            ToolSpec(name="active_tool", description="always allowed"),
            ToolSpec(name="find_tools", description="promotion entry"),
            ToolSpec(name="mcp__gh__issue", description="deferred", defer_loading=True),
        ],
    )
    call = client.calls[0]
    # Full schema set stays on the wire (prompt-cache friendly)…
    assert {t["function"]["name"] for t in call["tools"]} == {
        "active_tool",
        "find_tools",
        "mcp__gh__issue",
    }
    # …while the allowed subset excludes the marked (still-deferred) tool.
    choice = call["tool_choice"]
    assert choice["type"] == "allowed_tools"
    allowed = {t["function"]["name"] for t in choice["tools"]}
    assert allowed == {"active_tool", "find_tools"}


@pytest.mark.asyncio
async def test_no_markers_means_no_tool_choice() -> None:
    client = RecordingOpenAIClient(
        response={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )
    provider = OpenAIProvider(client=client, model="gpt-5.5")
    await provider.complete(
        messages=[HumanMessage(content="hi")],
        tools=[ToolSpec(name="active_tool", description="x")],
    )
    assert client.calls[0]["tool_choice"] is None


@pytest.mark.asyncio
async def test_allowed_tools_rejection_falls_back_and_sticks() -> None:
    """HX-J4 — a 4xx with the constraint resends once without it and the
    provider instance stays on the application tier afterwards."""
    from collections.abc import Mapping
    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class _RejectConstraintOnce:
        calls: list[dict[str, Any]] = field(default_factory=list)

        async def chat_completions(self, **kwargs: Any) -> Mapping[str, Any]:
            self.calls.append(kwargs)
            if kwargs.get("tool_choice") is not None:
                raise LLMClientError("openai 400: unknown tool_choice type")
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    client = _RejectConstraintOnce()
    provider = OpenAIProvider(client=client, model="gpt-5.5")
    deferred_tools = [ToolSpec(name="mcp__gh__issue", description="d", defer_loading=True)]

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=deferred_tools)
    assert result.content == "ok"
    assert client.calls[0]["tool_choice"] is not None
    assert client.calls[1]["tool_choice"] is None

    # Sticky fallback: next call goes straight out unconstrained.
    await provider.complete(messages=[HumanMessage(content="hi")], tools=deferred_tools)
    assert client.calls[2]["tool_choice"] is None


@pytest.mark.asyncio
async def test_plain_client_error_propagates_without_fallback() -> None:
    client = RecordingOpenAIClient(
        response={"choices": []},
        raise_with=LLMClientError("openai 400: bad request"),
    )
    provider = OpenAIProvider(client=client, model="gpt-5.5")
    with pytest.raises(LLMClientError):
        await provider.complete(
            messages=[HumanMessage(content="hi")],
            tools=[ToolSpec(name="active_tool", description="x")],
        )
    assert len(client.calls) == 1
