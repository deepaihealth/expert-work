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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest

from orchestrator.llm.embedder import _DEFAULT_TIMEOUT_S as _EMBED_TIMEOUT_S
from orchestrator.llm.embedder import HTTPEmbeddingClient
from orchestrator.llm.providers.anthropic import HTTPAnthropicClient
from orchestrator.llm.providers.openai import HTTPOpenAIClient
from orchestrator.llm.rerank import HTTPDashScopeRerankClient
from orchestrator.tools.sandbox import (
    _EXEC_HTTP_BUFFER_S,
    _MAX_EXEC_TIMEOUT_S,
    HTTPSupervisorClient,
)
from orchestrator.tools.web_search import SearXNGClient


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


# ---------------------------------------------------------------------------
# 命门,机器化版本 —— 六个 client 类的每一个 HTTP 调用点都必须显式传
# per-request timeout(共享 client 没有 client 级默认,见 app.py 的
# ``timeout=None`` — 漏一处就是该路径静默退到 httpx 内置 5s 默认)。上面
# 四个测试只覆盖 HTTPOpenAIClient;这里遍历全部 13 个真实调用点(openai
# 2 + anthropic 2 + embedder 1 + rerank 1 + web_search 1 + sandbox 6),
# 把"人读代码保证没漏"变成"删一处这里就红"。
#
# 自检(手工做过一次,见 PR 报告):临时删掉 sandbox.py 的
# ``exec()`` 或 openai.py 的 ``chat_completions`` 里的 ``timeout=`` 参数,
# 对应 case 从绿变红;恢复后复绿。
# ---------------------------------------------------------------------------

#: Distinctive per-instance timeout so "did the client thread its OWN
#: timeout_s through" is verifiable, not just "some timeout was passed".
_TIMEOUT_S = 12.5


def _timeout_dict(value: float | httpx.Timeout) -> dict[str, float | None]:
    t = value if isinstance(value, httpx.Timeout) else httpx.Timeout(value)
    return {"connect": t.connect, "read": t.read, "write": t.write, "pool": t.pool}


async def _openai_chat(shared: httpx.AsyncClient) -> None:
    client = HTTPOpenAIClient(api_key="k", http=shared, timeout_s=_TIMEOUT_S)
    await client.chat_completions(model="m", messages=[], tools=None)


async def _openai_stream(shared: httpx.AsyncClient) -> None:
    client = HTTPOpenAIClient(api_key="k", http=shared, timeout_s=_TIMEOUT_S)
    async for _ in client.stream_chat_completions(model="m", messages=[], tools=None):
        pass


async def _anthropic_messages(shared: httpx.AsyncClient) -> None:
    client = HTTPAnthropicClient(api_key="k", http=shared, timeout_s=_TIMEOUT_S)
    await client.messages(model="m", system=None, messages=[], tools=None, max_tokens=16)


async def _anthropic_stream(shared: httpx.AsyncClient) -> None:
    client = HTTPAnthropicClient(api_key="k", http=shared, timeout_s=_TIMEOUT_S)
    async for _ in client.stream_messages(
        model="m", system=None, messages=[], tools=None, max_tokens=16
    ):
        pass


async def _embedder(shared: httpx.AsyncClient) -> None:
    client = HTTPEmbeddingClient(api_key="k", http=shared)
    await client.embeddings(model="m", texts=["a"])


async def _rerank(shared: httpx.AsyncClient) -> None:
    client = HTTPDashScopeRerankClient(api_key="k", http=shared, timeout_s=_TIMEOUT_S)
    await client.rerank(model="m", query="q", documents=["d"], top_n=1)


async def _web_search(shared: httpx.AsyncClient) -> None:
    client = SearXNGClient(base_url="http://test", http=shared, timeout_s=_TIMEOUT_S)
    await client.search(query="q", max_results=5)


async def _sandbox_exec(shared: httpx.AsyncClient) -> None:
    client = HTTPSupervisorClient(base_url="http://test", http=shared, timeout_s=_TIMEOUT_S)
    await client.exec(sandbox_id=uuid4(), code="1+1", timeout_s=None)


async def _sandbox_read_workspace_file(shared: httpx.AsyncClient) -> None:
    client = HTTPSupervisorClient(base_url="http://test", http=shared, timeout_s=_TIMEOUT_S)
    await client.read_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path="p")


async def _sandbox_list_workspace_files(shared: httpx.AsyncClient) -> None:
    client = HTTPSupervisorClient(base_url="http://test", http=shared, timeout_s=_TIMEOUT_S)
    await client.list_workspace_files(tenant_id=uuid4(), user_id=uuid4())


async def _sandbox_write_workspace_file(shared: httpx.AsyncClient) -> None:
    client = HTTPSupervisorClient(base_url="http://test", http=shared, timeout_s=_TIMEOUT_S)
    await client.write_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path="p", data=b"x")


async def _sandbox_delete_workspace_file(shared: httpx.AsyncClient) -> None:
    client = HTTPSupervisorClient(base_url="http://test", http=shared, timeout_s=_TIMEOUT_S)
    await client.delete_workspace_file(tenant_id=uuid4(), user_id=uuid4(), path="p")


async def _sandbox_post(shared: httpx.AsyncClient) -> None:
    # release() is the simplest caller of the shared _post() helper
    # (expect_body=False — no response-shape assumptions needed); the same
    # timeout= line also covers acquire/destroy/reap/mark_workspace_deleted.
    client = HTTPSupervisorClient(base_url="http://test", http=shared, timeout_s=_TIMEOUT_S)
    await client.release(sandbox_id=uuid4())


@dataclass(frozen=True)
class _Case:
    case_id: str
    run: Callable[[httpx.AsyncClient], Awaitable[None]]
    expected_timeout: float | httpx.Timeout
    is_streaming: bool = False


_CASES = [
    _Case("openai.chat_completions", _openai_chat, _TIMEOUT_S),
    _Case(
        "openai.stream_chat_completions",
        _openai_stream,
        httpx.Timeout(_TIMEOUT_S, read=None),
        is_streaming=True,
    ),
    _Case("anthropic.messages", _anthropic_messages, _TIMEOUT_S),
    _Case(
        "anthropic.stream_messages",
        _anthropic_stream,
        httpx.Timeout(_TIMEOUT_S, read=None),
        is_streaming=True,
    ),
    _Case("embedder.embeddings", _embedder, _EMBED_TIMEOUT_S),
    _Case("rerank.rerank", _rerank, _TIMEOUT_S),
    _Case("web_search.search", _web_search, _TIMEOUT_S),
    _Case("sandbox.exec", _sandbox_exec, float(_MAX_EXEC_TIMEOUT_S) + _EXEC_HTTP_BUFFER_S),
    _Case("sandbox.read_workspace_file", _sandbox_read_workspace_file, _TIMEOUT_S),
    _Case("sandbox.list_workspace_files", _sandbox_list_workspace_files, _TIMEOUT_S),
    _Case("sandbox.write_workspace_file", _sandbox_write_workspace_file, _TIMEOUT_S),
    _Case("sandbox.delete_workspace_file", _sandbox_delete_workspace_file, _TIMEOUT_S),
    _Case("sandbox._post(release)", _sandbox_post, _TIMEOUT_S),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
async def test_every_call_site_passes_a_per_request_timeout(case: _Case) -> None:
    """机器化命门:每个调用点都必须显式传 per-request timeout,且必须复用
    共享 client(不关闭)。流式路径额外断言 ``read is None``。"""
    seen: list[object] = []

    class _Recorder(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout"))
            return httpx.Response(200, json={})

    shared = httpx.AsyncClient(transport=_Recorder())
    await case.run(shared)

    assert not shared.is_closed, f"{case.case_id}: shared client was closed"
    assert seen, f"{case.case_id}: no request captured"
    assert seen[0] == _timeout_dict(case.expected_timeout), (
        f"{case.case_id}: timeout mismatch — got {seen[0]!r}"
    )
    if case.is_streaming:
        assert isinstance(seen[0], dict)
        assert seen[0]["read"] is None, f"{case.case_id}: streaming path must not set read timeout"
