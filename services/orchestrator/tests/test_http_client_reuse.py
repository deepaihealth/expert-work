"""共享 httpx 客户端的注入与回退 —— 一期 Task 5。

原代码每次调用都 ``async with httpx.AsyncClient(...)`` —— 一次完整 TLS
握手。Task 5 给每个 HTTP client 类加一个可选的 ``http`` 字段:注入时复用
进程级共享 client(且绝不能被某次调用关闭),``None`` 时逐字节回退到原
per-call 行为。

第二个和第四个测试是这个 task 的命门:

- ``test_does_not_close_the_injected_client`` —— 原代码是 ``async with``,
  退出即关。注入的是进程级共享 client,被某一次调用顺手关掉的话,后续所有
  LLM 调用全炸,而且是运行时才炸,没有任何静态检查能抓住。
- ``test_streaming_path_keeps_the_no_read_timeout`` —— 流式那条原本是
  ``httpx.Timeout(self.timeout_s, read=None)``;走共享 client 后 timeout
  必须 per-request 传,否则 client 级默认 timeout 抢走 ``idle_timeout_s``
  的语义,长思考的模型会被误杀。
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator.llm.providers.openai import HTTPOpenAIClient


def _stub_transport() -> httpx.MockTransport:
    """A transport returning a fixed 200 JSON chat-completion body."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
        )

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_reuses_the_injected_client_across_calls() -> None:
    """注入 http 时,两次调用必须复用同一个 client 实例(不新建、不关闭)。"""
    shared = httpx.AsyncClient(transport=_stub_transport())
    client = HTTPOpenAIClient(api_key="k", http=shared)
    await client.chat_completions(model="m", messages=[], tools=None)
    await client.chat_completions(model="m", messages=[], tools=None)
    assert not shared.is_closed


@pytest.mark.asyncio
async def test_does_not_close_the_injected_client() -> None:
    """命门:原代码是 ``async with``,退出即关。注入的是进程级共享 client,
    被某一次调用关掉的话后续所有 LLM 调用全炸,而且是运行时才炸。"""
    shared = httpx.AsyncClient(transport=_stub_transport())
    client = HTTPOpenAIClient(api_key="k", http=shared)
    await client.chat_completions(model="m", messages=[], tools=None)
    assert not shared.is_closed


@pytest.mark.asyncio
async def test_falls_back_to_per_call_client_when_not_injected() -> None:
    """http=None(测试/eval CLI/未接线路径)行为与改造前一致。"""
    client = HTTPOpenAIClient(api_key="k", transport=_stub_transport())
    result = await client.chat_completions(model="m", messages=[], tools=None)
    assert result is not None


@pytest.mark.asyncio
async def test_streaming_path_keeps_the_no_read_timeout() -> None:
    """流式那条原本是 ``httpx.Timeout(self.timeout_s, read=None)``;走共享
    client 后 timeout 必须 per-request 传,否则 idle_timeout_s 的语义被
    client 级 timeout 抢走,长思考的模型会被误杀。"""
    seen: list[object] = []

    class _RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout"))
            return httpx.Response(200, text="data: [DONE]\n\n")

    shared = httpx.AsyncClient(transport=_RecordingTransport())
    client = HTTPOpenAIClient(api_key="k", http=shared, timeout_s=30.0)
    async for _ in client.stream_chat_completions(model="m", messages=[], tools=None):
        pass

    # httpx 把 Timeout 摊平成 extensions["timeout"] 的 dict,read=None 表示
    # 不设读超时 —— 这正是流式路径依赖的语义。
    assert seen
    timeout = seen[0]
    assert isinstance(timeout, dict)
    assert timeout["read"] is None
