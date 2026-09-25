"""Unit tests for :class:`AnthropicProvider` + :class:`HTTPAnthropicClient`
— Stream E.11.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from expert_work.persistence import (
    RENDERED_FIGURE_DIR,
    RENDERED_FIGURE_PAGE_STEM,
    RENDERED_FIGURE_SHA_HEX_LEN,
    RENDERED_FIGURE_UNIT_PREFIX,
    WORKSPACE_OVERFLOW_DIR,
)
from expert_work.runtime.middleware import (
    LLMClientError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
)
from orchestrator.llm import (
    AnthropicProvider,
    HTTPAnthropicClient,
    RecordingAnthropicClient,
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
async def test_system_message_lifted_to_system_field() -> None:
    """Pre-L1 wire shape — exercised with ``cache_enabled=False``.
    The string-shaped ``system`` field still works when caching is
    explicitly disabled. The L1-enabled shape is in
    ``test_cache_control_applied_to_system_and_trailing_messages``."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude-sonnet-4-6", cache_enabled=False)

    await provider.complete(
        messages=[
            SystemMessage(content="you are helpful"),
            HumanMessage(content="hi"),
        ],
        tools=[],
    )

    assert client.calls[0]["system"] == "you are helpful"
    assert client.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_multiple_system_messages_concatenated() -> None:
    """Multiple system messages still concat to a single string when
    L1 cache is disabled — exercises the string fallback shape."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude", cache_enabled=False)

    await provider.complete(
        messages=[
            SystemMessage(content="rule 1"),
            SystemMessage(content="rule 2"),
            HumanMessage(content="go"),
        ],
        tools=[],
    )

    assert client.calls[0]["system"] == "rule 1\n\nrule 2"


@pytest.mark.asyncio
async def test_no_system_message_leaves_system_none() -> None:
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["system"] is None


@pytest.mark.asyncio
async def test_ai_message_with_tool_calls_emits_tool_use_block() -> None:
    """Exercises the assistant + tool_result wire shape with caching
    off so the assertions stay focused on the message mapping
    logic; L1 cache-control markers are covered separately."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "done"}]},
    )
    provider = AnthropicProvider(client=client, model="claude", cache_enabled=False)

    history_ai = AIMessage(
        content="checking",
        tool_calls=[
            {
                "id": "toolu_1",
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
            ToolMessage(content="127.0.0.1 localhost", tool_call_id="toolu_1"),
        ],
        tools=[],
    )

    assistant_msg = client.calls[0]["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == [
        {"type": "text", "text": "checking"},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "read_file",
            "input": {"path": "/etc/hosts"},
        },
    ]

    tool_msg = client.calls[0]["messages"][2]
    assert tool_msg == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "127.0.0.1 localhost",
            }
        ],
    }


@pytest.mark.asyncio
async def test_tool_specs_mapped_to_input_schema() -> None:
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")
    spec = ToolSpec(
        name="search",
        description="search the web",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[spec])

    assert client.calls[0]["tools"] == [
        {
            "name": "search",
            "description": "search the web",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]


@pytest.mark.asyncio
async def test_empty_tools_yields_no_tools_field() -> None:
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["tools"] is None


@pytest.mark.asyncio
async def test_temperature_passed_to_client() -> None:
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(client=client, model="claude", temperature=0.3)

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["temperature"] == 0.3


@pytest.mark.asyncio
async def test_temperature_defaults_to_none() -> None:
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(client=client, model="claude")

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["temperature"] is None


@pytest.mark.asyncio
async def test_thinking_enabled_false_sends_disabled_and_drops_effort() -> None:
    # Thinking-Toggle — explicit off forces thinking:{type:disabled} and drops
    # output_config.effort even if an effort level is also set.
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(
        client=client, model="claude", effort="high", thinking_enabled=False
    )

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["thinking"] == {"type": "disabled"}
    assert client.calls[0]["output_config"] is None


@pytest.mark.asyncio
async def test_thinking_enabled_true_keeps_effort_and_adaptive() -> None:
    # On (or inherit) keeps the CM-9 effort + adaptive behaviour unchanged.
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(
        client=client,
        model="claude",
        effort="medium",
        adaptive_thinking=True,
        thinking_enabled=True,
    )

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["thinking"] == {"type": "adaptive"}
    assert client.calls[0]["output_config"] == {"effort": "medium"}


# ---------------------------------------------------------------------------
# Response decoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_only_response_yields_plain_ai_message() -> None:
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "hello world"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    result = await provider.complete(
        messages=[HumanMessage(content="hi")],
        tools=[],
    )

    assert result.content == "hello world"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_tool_use_response_decoded_to_tool_calls() -> None:
    client = RecordingAnthropicClient(
        response={
            "content": [
                {"type": "text", "text": "let me check"},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "search",
                    "input": {"q": "hello"},
                },
            ]
        },
    )
    provider = AnthropicProvider(client=client, model="claude")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.content == "let me check"
    assert result.tool_calls == [
        {"id": "toolu_abc", "name": "search", "args": {"q": "hello"}, "type": "tool_call"},
    ]


@pytest.mark.asyncio
async def test_multiple_tool_use_blocks_decoded_in_order() -> None:
    client = RecordingAnthropicClient(
        response={
            "content": [
                {"type": "tool_use", "id": "1", "name": "a", "input": {}},
                {"type": "tool_use", "id": "2", "name": "b", "input": {"x": 1}},
            ]
        },
    )
    provider = AnthropicProvider(client=client, model="claude")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert [tc["name"] for tc in result.tool_calls] == ["a", "b"]
    assert result.tool_calls[1]["args"] == {"x": 1}


@pytest.mark.asyncio
async def test_empty_response_yields_empty_ai_message() -> None:
    client = RecordingAnthropicClient(response={})
    provider = AnthropicProvider(client=client, model="claude")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.content == ""
    assert result.tool_calls == []


# ---------------------------------------------------------------------------
# HTTPAnthropicClient — error mapping
# ---------------------------------------------------------------------------


def _http_client(handler) -> HTTPAnthropicClient:  # type: ignore[no-untyped-def]
    """Build an :class:`HTTPAnthropicClient` wired to ``handler`` via
    :class:`httpx.MockTransport` so tests exercise the real error-mapping
    code path without hitting the network."""
    return HTTPAnthropicClient(
        api_key="sk-test",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )


async def _call(client: HTTPAnthropicClient) -> object:
    return await client.messages(
        model="claude",
        system=None,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=100,
    )


@pytest.mark.asyncio
async def test_http_429_raises_rate_limit_error() -> None:
    client = _http_client(lambda _req: httpx.Response(429, text="rate_limited"))
    with pytest.raises(LLMRateLimitError, match="429"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_400_raises_client_error() -> None:
    client = _http_client(lambda _req: httpx.Response(400, text="bad_request"))
    with pytest.raises(LLMClientError, match="400"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_503_raises_server_error() -> None:
    client = _http_client(lambda _req: httpx.Response(503, text="unavailable"))
    with pytest.raises(LLMServerError, match="503"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_network_error_raises_network_error() -> None:
    """Raw :class:`httpx.HTTPError` is mapped to :class:`LLMNetworkError`."""

    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _http_client(_boom)
    with pytest.raises(LLMNetworkError, match="connection"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_200_returns_parsed_json() -> None:
    client = _http_client(
        lambda _req: httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]}),
    )
    body = await _call(client)
    assert body == {"content": [{"type": "text", "text": "ok"}]}


@pytest.mark.asyncio
async def test_http_temperature_included_in_request_body() -> None:
    captured: dict[str, object] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _http_client(_handler)
    await client.messages(
        model="claude",
        system=None,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=100,
        temperature=0.3,
    )
    assert captured["temperature"] == 0.3
    # ``None`` keeps it out of the body entirely.
    captured.clear()
    await client.messages(
        model="claude",
        system=None,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=100,
    )
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_http_200_non_object_body_raises_server_error() -> None:
    client = _http_client(lambda _req: httpx.Response(200, json=["unexpected", "list"]))
    with pytest.raises(LLMServerError, match="non-object"):
        await _call(client)


@pytest.mark.asyncio
async def test_http_500_error_body_truncated_to_500_chars() -> None:
    """Verbose 5xx bodies should not blow up logs / exception messages."""
    huge = "x" * 2000
    client = _http_client(lambda _req: httpx.Response(500, text=huge))
    with pytest.raises(LLMServerError) as exc_info:
        await _call(client)
    assert "...[truncated]" in str(exc_info.value)
    # Truncation cap is 500 chars; full exception string is bounded.
    assert len(str(exc_info.value)) < 1000


# ---------------------------------------------------------------------------
# Recording client error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_client_raises_injected_exception() -> None:
    client = RecordingAnthropicClient(raise_with=LLMServerError("simulated"))
    provider = AnthropicProvider(client=client, model="claude")

    with pytest.raises(LLMServerError, match="simulated"):
        await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    # The call should still have been recorded — useful for asserting
    # that the request was constructed before the simulated failure.
    assert len(client.calls) == 1


def test_recording_response_default_is_empty_dict() -> None:
    client = RecordingAnthropicClient()
    assert client.response == {}
    assert client.calls == []
    # Confidence check: json roundtrip works on the default response.
    json.dumps(dict(client.response))


# ---------------------------------------------------------------------------
# Multimodal — image_ref content blocks (Stream J.6 Path A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_image_ref_emits_base64_image_block() -> None:
    uri = "expert_work://image/demo.png"
    resolver = InMemoryImageResolver(
        images={uri: ResolvedImage(media_type="image/png", data=b"PNGBYTES")}
    )
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(client=client, model="claude-sonnet-4-5", image_resolver=resolver)

    await provider.complete(
        messages=[
            HumanMessage(content=[{"type": "text", "text": "what is this?"}, image_ref_block(uri)])
        ],
        tools=[],
    )

    content = client.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["data"]


@pytest.mark.asyncio
async def test_human_image_ref_dropped_without_resolver() -> None:
    uri = "expert_work://image/demo.png"
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    # cache_enabled=False so the assertion checks the raw mapped
    # content (caching wraps strings in block lists with cache_control).
    provider = AnthropicProvider(
        client=client, model="claude-sonnet-4-5", cache_enabled=False
    )  # no image_resolver

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
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(client=client, model="claude-sonnet-4-5", image_resolver=resolver)

    await provider.complete(
        messages=[
            HumanMessage(content=[{"type": "text", "text": "what is this?"}, image_ref_block(uri)])
        ],
        tools=[],
    )

    content = client.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["data"]


# ---------------------------------------------------------------------------
# B-64 Task 7 回修 —— 工作区图读不出来时降级成文字,不让整次调用失败
# ---------------------------------------------------------------------------


def _rendered_page(root: Path, tenant: UUID, user: UUID, page: int, *, on_disk: bool) -> str:
    """在 ``root`` 下按 ``read_page`` 的真实落点形状放一页(或故意不放),返回它的 ref。"""
    rel = "/".join(
        [
            WORKSPACE_OVERFLOW_DIR,
            str(uuid4()),
            RENDERED_FIGURE_DIR,
            "a" * RENDERED_FIGURE_SHA_HEX_LEN,
            "b" * RENDERED_FIGURE_SHA_HEX_LEN,
            f"{RENDERED_FIGURE_UNIT_PREFIX}{page}",
            f"{RENDERED_FIGURE_PAGE_STEM}-{page:02d}.jpg",
        ]
    )
    if on_disk:
        path = root / str(tenant) / str(user) / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return f"expert_work://workspace/{tenant}/{user}/{rel}"


def _workspace_resolver(root: Path) -> DispatchingImageResolver:
    return DispatchingImageResolver(
        uploads=InMemoryImageResolver({}), workspace=NasWorkspaceImageResolver(root=root)
    )


@pytest.mark.asyncio
async def test_a_missing_workspace_image_degrades_to_text_in_its_place(tmp_path: Path) -> None:
    """留存清理删掉了 ``.tool_results/<run_id>/``:那张图换成文字,另外两张照常。

    不降级的话这里抛 ``FileNotFoundError`` —— 而同一条 ref 下一轮还在,会话从此
    每一轮都失败。
    """
    tenant, user = uuid4(), uuid4()
    refs = [
        _rendered_page(tmp_path, tenant, user, 3, on_disk=True),
        _rendered_page(tmp_path, tenant, user, 5, on_disk=False),
        _rendered_page(tmp_path, tenant, user, 7, on_disk=True),
    ]
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(
        client=client, model="claude-sonnet-4-5", image_resolver=_workspace_resolver(tmp_path)
    )

    await provider.complete(
        messages=[
            HumanMessage(
                content=[{"type": "text", "text": "看图"}, *(image_ref_block(r) for r in refs)]
            )
        ],
        tools=[],
    )

    content = client.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image"
    assert content[2]["type"] == "text"
    assert "第 5 页" in content[2]["text"]
    assert "read_page" in content[2]["text"]
    assert content[3]["type"] == "image"
    assert len(content) == 4


@pytest.mark.asyncio
async def test_every_workspace_image_missing_still_calls_the_model(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    refs = [_rendered_page(tmp_path, tenant, user, p, on_disk=False) for p in (3, 5, 7)]
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(
        client=client, model="claude-sonnet-4-5", image_resolver=_workspace_resolver(tmp_path)
    )

    await provider.complete(
        messages=[
            HumanMessage(
                content=[{"type": "text", "text": "看图"}, *(image_ref_block(r) for r in refs)]
            )
        ],
        tools=[],
    )

    content = client.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "text", "text", "text"]
    assert ["第 3 页" in content[1]["text"], "第 5 页" in content[2]["text"]] == [True, True]
    assert "第 7 页" in content[3]["text"]


@pytest.mark.asyncio
async def test_an_upload_ref_that_fails_to_resolve_still_raises(tmp_path: Path) -> None:
    """回归钉:上传图的失败行为不在这次改动范围里 —— 异常原样抛出,一个字不改。"""
    uri = f"expert_work://image/{uuid4()}/{uuid4()}/{uuid4()}.png"
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(
        client=client, model="claude-sonnet-4-5", image_resolver=_workspace_resolver(tmp_path)
    )

    with pytest.raises(KeyError, match="no image for ref"):
        await provider.complete(
            messages=[HumanMessage(content=[{"type": "text", "text": "hi"}, image_ref_block(uri)])],
            tools=[],
        )
    assert client.calls == []


# ---------------------------------------------------------------------------
# Stream L.L1 — Anthropic prompt caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_control_applied_to_system_when_enabled() -> None:
    """L1 default: ``cache_enabled=True`` (the AnthropicProvider's
    constructor default) wraps the ``system`` field as a one-element
    block list with an ephemeral ``cache_control`` marker so the
    upstream caches the system prompt prefix."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude-sonnet-4-6")

    await provider.complete(
        messages=[
            SystemMessage(content="you are helpful"),
            HumanMessage(content="hi"),
        ],
        tools=[],
    )

    system_block = client.calls[0]["system"]
    assert system_block == [
        {
            "type": "text",
            "text": "you are helpful",
            "cache_control": {"type": "ephemeral"},
        }
    ]


@pytest.mark.asyncio
async def test_cache_control_marks_trailing_two_messages() -> None:
    """L1 layout + Capability Uplift Sprint #8 (Mini-ADR U-7):
    ``_CACHE_CONTROL_TAIL_COUNT`` dropped from 3 to 2 to make room
    for the Sprint #8 memory anchor breakpoint (system + tail-2 +
    anchor ≤ Anthropic's 4-breakpoint cap)."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    await provider.complete(
        messages=[
            HumanMessage(content="m1"),
            AIMessage(content="m2"),
            HumanMessage(content="m3"),
            AIMessage(content="m4"),
            HumanMessage(content="m5"),
        ],
        tools=[],
    )

    msgs = client.calls[0]["messages"]
    # First three messages stay un-marked (only the last two carry markers).
    assert msgs[0]["content"] == "m1"
    assert msgs[1]["content"] == "m2"
    assert msgs[2]["content"] == "m3"
    # The trailing two messages have their content lifted to a block
    # list with a cache_control marker on the final block.
    for tail_msg in msgs[3:]:
        content = tail_msg["content"]
        assert isinstance(content, list)
        assert content[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_control_marks_existing_block_list_in_place() -> None:
    """An assistant message that already carries a block list (J.6
    multimodal / tool_use) gets the marker on its terminal block
    rather than getting wrapped in another block list."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")
    ai_with_tool_call = AIMessage(
        content="checking",
        tool_calls=[{"id": "tu_1", "name": "search", "args": {}, "type": "tool_call"}],
    )

    await provider.complete(
        messages=[HumanMessage(content="hi"), ai_with_tool_call],
        tools=[],
    )

    msgs = client.calls[0]["messages"]
    # Assistant message had blocks [text, tool_use]; the tool_use block
    # is the terminal one and gets the cache_control marker. The text
    # block stays unchanged.
    assistant_content = msgs[1]["content"]
    assert assistant_content[0] == {"type": "text", "text": "checking"}
    assert assistant_content[-1]["type"] == "tool_use"
    assert assistant_content[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_disabled_emits_string_system_and_unmarked_messages() -> None:
    """L1 opt-out path: ``cache_enabled=False`` produces the legacy
    string-shape ``system`` and leaves message content untouched. The
    agent factory wires this from ``ModelSpec.cache_enabled``."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude", cache_enabled=False)

    await provider.complete(
        messages=[
            SystemMessage(content="be terse"),
            HumanMessage(content="hi"),
        ],
        tools=[],
    )

    # No block-list wrapper, no cache_control anywhere.
    assert client.calls[0]["system"] == "be terse"
    assert client.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_cache_control_no_system_just_messages() -> None:
    """When there's no SystemMessage the outbound ``system`` field stays
    ``None`` even with caching enabled — the marker hangs off the
    trailing messages only."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert client.calls[0]["system"] is None
    # The single message picks up the cache marker (trailing window of
    # size 2 captures it).
    assert client.calls[0]["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Capability Uplift Sprint #8 — cache anchor (Mini-ADR U-7)
# ---------------------------------------------------------------------------


def _count_cache_markers(call: dict[str, Any]) -> int:
    """Count every ``cache_control`` marker in the outbound call body
    (system block + message content blocks)."""
    n = 0
    system = call.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and "cache_control" in block:
                n += 1
    for msg in call.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    n += 1
    return n


@pytest.mark.asyncio
async def test_cache_anchor_marks_expert_work_cache_anchor_message() -> None:
    """Sprint #8 Mini-ADR U-7: a message whose ``additional_kwargs``
    carry ``expert_work_cache_anchor: True`` gets a ``cache_control`` marker
    on its terminal content block — even if it sits well outside the
    trailing tail window."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    memory_block = HumanMessage(
        content="user prefers concise replies",
        additional_kwargs={"expert_work_cache_anchor": True},
    )

    await provider.complete(
        messages=[
            HumanMessage(content="task"),
            memory_block,
            AIMessage(content="thinking"),
            HumanMessage(content="m3"),
            AIMessage(content="m4"),
            HumanMessage(content="m5"),
        ],
        tools=[],
    )

    msgs = client.calls[0]["messages"]
    # Anchor at index 1 — far outside the trailing two-message window
    # (which starts at index 4) — still picks up the marker.
    anchor = msgs[1]["content"]
    assert isinstance(anchor, list)
    assert anchor[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_anchor_total_markers_within_anthropic_cap() -> None:
    """Sprint #8 Mini-ADR U-7: system (1) + tail-2 (2) + memory anchor
    (1) ≤ 4 ≤ Anthropic's documented per-request cache_control cap."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    memory_block = HumanMessage(
        content="memory",
        additional_kwargs={"expert_work_cache_anchor": True},
    )

    await provider.complete(
        messages=[
            SystemMessage(content="system"),
            HumanMessage(content="task"),
            memory_block,
            AIMessage(content="a1"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
            HumanMessage(content="h3"),
        ],
        tools=[],
    )

    assert _count_cache_markers(client.calls[0]) <= 4


@pytest.mark.asyncio
async def test_cache_anchor_inside_tail_window_does_not_double_mark() -> None:
    """An anchor that already sits in the trailing window gets exactly
    one marker, not two — the cap-counting must dedup."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    short_session = HumanMessage(
        content="memory",
        additional_kwargs={"expert_work_cache_anchor": True},
    )

    await provider.complete(
        messages=[
            HumanMessage(content="task"),
            short_session,
        ],
        tools=[],
    )

    msgs = client.calls[0]["messages"]
    anchor_blocks = msgs[1]["content"]
    assert isinstance(anchor_blocks, list)
    # Exactly one cache_control marker on the anchor, not two.
    marker_blocks = [b for b in anchor_blocks if isinstance(b, dict) and "cache_control" in b]
    assert len(marker_blocks) == 1


@pytest.mark.asyncio
async def test_cache_anchor_skipped_when_caching_disabled() -> None:
    """``cache_enabled=False`` disables the whole feature including
    the Sprint #8 anchor — a per-model opt-out remains absolute."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude", cache_enabled=False)

    memory_block = HumanMessage(
        content="memory",
        additional_kwargs={"expert_work_cache_anchor": True},
    )

    await provider.complete(
        messages=[
            HumanMessage(content="task"),
            memory_block,
            AIMessage(content="a"),
        ],
        tools=[],
    )

    # No system block; messages are plain strings (no block wrapping);
    # no markers anywhere.
    assert _count_cache_markers(client.calls[0]) == 0


@pytest.mark.asyncio
async def test_response_usage_carries_cache_token_counters() -> None:
    """When the upstream returns cache-aware token counters, the
    decoder propagates them onto ``AIMessage.usage_metadata`` so
    observability middleware (E.5 langfuse) can split cached vs
    uncached input tokens."""
    client = RecordingAnthropicClient(
        response={
            "content": [{"type": "text", "text": "hello"}],
            "usage": {
                "input_tokens": 8,
                "output_tokens": 3,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 500,
            },
        },
    )
    provider = AnthropicProvider(client=client, model="claude")

    response = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    usage = response.usage_metadata
    assert usage is not None
    assert usage["input_tokens"] == 8
    assert usage["output_tokens"] == 3
    assert usage["total_tokens"] == 11
    details = usage.get("input_token_details") or {}
    assert details["cache_creation"] == 100
    assert details["cache_read"] == 500


@pytest.mark.asyncio
async def test_response_without_usage_returns_message_without_metadata() -> None:
    """Backward-compat: an Anthropic response without ``usage`` (or
    with all-missing counters) does not crash the decoder; the
    returned AIMessage simply has no ``usage_metadata``."""
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "hi"}]},
    )
    provider = AnthropicProvider(client=client, model="claude")

    response = await provider.complete(messages=[HumanMessage(content="ping")], tools=[])

    assert response.usage_metadata is None


# ---------------------------------------------------------------------------
# Stream CM-9 — compute-control fields (thinking / output_config / sampling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defaults_send_no_compute_control_fields() -> None:
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(client=client, model="claude-sonnet-4-6", cache_enabled=False)
    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])
    assert client.calls[0]["thinking"] is None
    assert client.calls[0]["output_config"] is None


@pytest.mark.asyncio
async def test_effort_and_adaptive_thinking_on_the_wire() -> None:
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "ok"}]})
    provider = AnthropicProvider(
        client=client,
        model="claude-opus-4-8",
        cache_enabled=False,
        effort="high",
        adaptive_thinking=True,
    )
    await provider.complete(messages=[HumanMessage(content="hi")], tools=[])
    assert client.calls[0]["thinking"] == {"type": "adaptive"}
    assert client.calls[0]["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_http_body_carries_thinking_and_effort_and_omits_temperature() -> None:
    """Wire-level shape via MockTransport — the body the API actually sees."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    http_client = HTTPAnthropicClient(api_key="k", transport=httpx.MockTransport(handler))
    await http_client.messages(
        model="claude-opus-4-8",
        system=None,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=64,
        temperature=None,
        thinking={"type": "adaptive"},
        output_config={"effort": "max"},
    )
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": "max"}
    assert "temperature" not in captured


# --- Stream HX-13 — native_search tier (defer_loading + beta header) --------


@pytest.mark.asyncio
async def test_defer_loading_marker_ships_with_beta_header() -> None:
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude-opus-4-8")
    await provider.complete(
        messages=[HumanMessage(content="hi")],
        tools=[
            ToolSpec(name="active_tool", description="always bound"),
            ToolSpec(name="mcp__gh__issue", description="deferred", defer_loading=True),
        ],
    )
    call = client.calls[0]
    assert call["betas"] == ["tool-search-tool-2025-10-19"]
    by_name = {t["name"]: t for t in call["tools"]}
    assert by_name["mcp__gh__issue"]["defer_loading"] is True
    assert "defer_loading" not in by_name["active_tool"]


@pytest.mark.asyncio
async def test_no_markers_means_no_beta_header() -> None:
    client = RecordingAnthropicClient(
        response={"content": [{"type": "text", "text": "ok"}]},
    )
    provider = AnthropicProvider(client=client, model="claude-opus-4-8")
    await provider.complete(
        messages=[HumanMessage(content="hi")],
        tools=[ToolSpec(name="active_tool", description="always bound")],
    )
    assert client.calls[0]["betas"] is None


@pytest.mark.asyncio
async def test_beta_rejection_falls_back_and_sticks() -> None:
    """HX-J4 — a 4xx on the beta request resends once without markers and
    the provider instance stays on the application tier afterwards."""

    @dataclass
    class _RejectBetaOnce:
        calls: list[dict[str, Any]] = field(default_factory=list)

        async def messages(self, **kwargs: Any) -> Mapping[str, Any]:
            self.calls.append(kwargs)
            if kwargs.get("betas"):
                raise LLMClientError("anthropic 400: unknown beta")
            return {"content": [{"type": "text", "text": "ok"}]}

    client = _RejectBetaOnce()
    provider = AnthropicProvider(client=client, model="claude-opus-4-8")
    deferred_tools = [
        ToolSpec(name="mcp__gh__issue", description="deferred", defer_loading=True),
    ]

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=deferred_tools)
    assert result.content == "ok"
    # First call carried the beta; the resend dropped markers + header.
    assert client.calls[0]["betas"] == ["tool-search-tool-2025-10-19"]
    assert client.calls[1]["betas"] is None
    assert "defer_loading" not in client.calls[1]["tools"][0]

    # Sticky: the next complete() goes straight to the plain shape.
    await provider.complete(messages=[HumanMessage(content="hi")], tools=deferred_tools)
    assert client.calls[2]["betas"] is None
    assert "defer_loading" not in client.calls[2]["tools"][0]


@pytest.mark.asyncio
async def test_plain_client_error_propagates_without_fallback() -> None:
    client = RecordingAnthropicClient(
        response={"content": []},
        raise_with=LLMClientError("anthropic 400: bad request"),
    )
    provider = AnthropicProvider(client=client, model="claude-opus-4-8")
    with pytest.raises(LLMClientError):
        await provider.complete(
            messages=[HumanMessage(content="hi")],
            tools=[ToolSpec(name="active_tool", description="x")],
        )
    assert len(client.calls) == 1  # no resend on the non-beta path


# ---------------------------------------------------------------------------
# B-105 —— stop_reason 进 response_metadata(截断判定读它)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {"input_tokens": 3, "output_tokens": 2}])
async def test_stop_reason_lands_in_response_metadata(usage: dict[str, int] | None) -> None:
    body: dict[str, Any] = {
        "content": [{"type": "text", "text": "partial"}],
        "stop_reason": "max_tokens",
    }
    if usage is not None:
        body["usage"] = usage
    provider = AnthropicProvider(client=RecordingAnthropicClient(response=body), model="claude")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert result.response_metadata["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_missing_stop_reason_leaves_metadata_without_it() -> None:
    client = RecordingAnthropicClient(response={"content": [{"type": "text", "text": "x"}]})
    provider = AnthropicProvider(client=client, model="claude")

    result = await provider.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert "stop_reason" not in result.response_metadata
