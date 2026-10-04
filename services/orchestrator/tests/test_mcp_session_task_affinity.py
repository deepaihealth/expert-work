"""Repro: an MCP session opened lazily inside a request leaks its anyio scopes.

Production symptom (test cluster, image ``fc07b8b1``): the first streaming run
on a fresh pod finishes ``success`` and *then* uvicorn logs
``RuntimeError: Attempted to exit a cancel scope that isn't the current tasks's
current cancel scope`` from ``BaseHTTPMiddleware``; the client sees
``ReadError``.

Mechanism: ``_open_session`` enters the transport + ``ClientSession`` (each
holds an anyio task group) on an ``AsyncExitStack`` and *returns with the
stack still open*. When that first happens inside a request (lazy
tenant/platform MCP pool build, TTL rebuild, BUG-17 ``_restart``), those cancel
scopes stay pushed on the request task's scope stack. anyio >= 4.14 wraps every
task-group child in its own ``TaskHandle`` cancel scope, whose exit at task end
now finds a foreign scope on top -> RuntimeError.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

import orchestrator.tools.mcp as mcp_mod

_SERVER_TASKS: set[asyncio.Task[None]] = set()


def _fastmcp() -> FastMCP:
    srv = FastMCP("t")

    @srv.tool()
    def echo(text: str) -> str:
        return text

    return srv


@dataclass
class _InMemoryMCPClient(mcp_mod._RemoteMCPClientBase):
    """Real ``ClientSession`` over anyio memory streams; server in its own task."""

    _server: Any = field(default_factory=_fastmcp, init=False, repr=False)

    async def _open_streams(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        c2s_send, c2s_recv = anyio.create_memory_object_stream[Any](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[Any](16)
        low = self._server._mcp_server
        task = asyncio.create_task(low.run(c2s_recv, s2c_send, low.create_initialization_options()))
        _SERVER_TASKS.add(task)
        task.add_done_callback(_SERVER_TASKS.discard)
        return s2c_recv, c2s_send


class _Passthrough(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        return await call_next(request)


def _app(holder: dict[str, Any]) -> FastAPI:
    app = FastAPI()

    @app.post("/run")
    async def run(request: Request) -> StreamingResponse:
        if "client" not in holder:  # lazy first-use build, like TenantMCPPool
            client = _InMemoryMCPClient(
                config=mcp_mod.MCPServerConfig(
                    name="t", transport="streamable_http", url="http://x/mcp"
                )
            )
            await client.start()
            holder["client"] = client
        tools = await holder["client"].list_tools()

        async def body() -> AsyncIterator[bytes]:
            if await request.is_disconnected():
                return
            yield f"data: {len(tools)}\n\n".encode()
            yield b"event: end\ndata: {}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    app.add_middleware(_Passthrough)
    return app


@pytest.mark.timeout(30)
async def test_lazy_mcp_session_in_request_does_not_break_middleware() -> None:
    holder: dict[str, Any] = {}
    transport = httpx.ASGITransport(app=_app(holder))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        # First request opens the session inside the request task.
        r1 = await http.post("/run", json={})
        assert r1.status_code == 200
        assert "event: end" in r1.text
        # Second request reuses the cached session.
        r2 = await http.post("/run", json={})
        assert r2.status_code == 200
        # The session must still be usable after the opening request ended.
        tools = await holder["client"].list_tools()
        assert [t.name for t in tools] == ["echo"]
    # Pool close / invalidate runs in yet another task.
    await holder["client"].close()
    for t in list(_SERVER_TASKS):
        t.cancel()


@pytest.mark.timeout(30)
async def test_cancelled_open_closes_the_stack_and_leaves_nothing_behind() -> None:
    """The caller is cancelled while the owner task is still opening: the
    owner must exit what it opened and finish, and no exception may be left
    on a future nobody reads (asyncio logs those as errors at GC)."""
    import gc

    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
    closed: list[bool] = []
    opening = asyncio.Event()

    async def slow_open(stack: contextlib.AsyncExitStack) -> Any:
        stack.callback(closed.append, True)
        opening.set()
        await asyncio.sleep(10)

    try:
        opener = asyncio.create_task(mcp_mod._open_owned_session(slow_open))
        await opening.wait()
        opener.cancel()
        await asyncio.wait({opener})
        assert opener.cancelled()
        del opener
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert closed == [True]
    assert [c.get("message") for c in seen] == []
