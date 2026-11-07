"""B-297 — a vendor ``Retry-After`` header reaches the raised error.

Exercises every HTTP call site of the shared classifier (OpenAI-wire and
Anthropic, buffered and streaming) through ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx
import pytest

from expert_work.runtime.middleware import LLMRateLimitError, LLMServerError
from orchestrator.llm.providers.anthropic import HTTPAnthropicClient
from orchestrator.llm.providers.openai import HTTPOpenAIClient


def _transport(status: int, headers: dict[str, str]) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, text="busy")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_openai_buffered_carries_retry_after() -> None:
    client = HTTPOpenAIClient(api_key="k", transport=_transport(429, {"Retry-After": "6"}))
    with pytest.raises(LLMRateLimitError) as info:
        await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}], tools=None
        )
    assert info.value.retry_after_s == 6.0


@pytest.mark.asyncio
async def test_openai_stream_carries_retry_after_ms() -> None:
    client = HTTPOpenAIClient(api_key="k", transport=_transport(503, {"retry-after-ms": "2500"}))
    with pytest.raises(LLMServerError) as info:
        async for _ in client.stream_chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}], tools=None
        ):
            pass
    assert info.value.retry_after_s == 2.5


@pytest.mark.asyncio
async def test_anthropic_buffered_carries_retry_after() -> None:
    client = HTTPAnthropicClient(api_key="k", transport=_transport(529, {"retry-after": "11"}))
    with pytest.raises(LLMServerError) as info:
        await client.messages(
            model="c",
            system=None,
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=10,
        )
    assert info.value.retry_after_s == 11.0


@pytest.mark.asyncio
async def test_anthropic_stream_carries_retry_after() -> None:
    client = HTTPAnthropicClient(api_key="k", transport=_transport(429, {"retry-after": "3"}))
    with pytest.raises(LLMRateLimitError) as info:
        async for _ in client.stream_messages(
            model="c",
            system=None,
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=10,
        ):
            pass
    assert info.value.retry_after_s == 3.0
