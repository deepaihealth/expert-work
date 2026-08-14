"""Capability Uplift Sprint #5 — real-server e2e.

Spins up an in-process :class:`FastMCP` server on a free port, mounts it
under both SSE and StreamableHTTP ASGI apps via uvicorn, and verifies
the new :class:`SseMCPClient` / :class:`StreamableHttpMCPClient` round-
trip ``initialize`` → ``list_tools`` → ``call_tool`` against real wire
traffic. The MCP SDK is the same dependency the production client uses
(``mcp>=1.0,<2``), so a green run here covers Sprint #5 § 6.9 verification
clause 1 without depending on a public hosted endpoint.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from orchestrator.tools import MCPServerConfig
from orchestrator.tools.mcp import SseMCPClient, StreamableHttpMCPClient


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_test_server() -> FastMCP:
    server = FastMCP(name="expert-work-e2e")

    @server.tool()
    def echo(text: str) -> str:
        """Return the input string."""
        return text

    @server.tool()
    def add(a: int, b: int) -> int:
        """Return a + b."""
        return a + b

    return server


@asynccontextmanager
async def _run_uvicorn(app: object, port: int) -> AsyncIterator[None]:
    """Run ``app`` on 127.0.0.1:port for the duration of the context."""
    config = uvicorn.Config(
        app,  # type: ignore[arg-type]
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # Wait for uvicorn to mark itself ready.
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        server.should_exit = True
        _ = await task
        msg = "uvicorn did not start within 5 seconds"
        raise RuntimeError(msg)
    try:
        yield
    finally:
        # Bounded teardown: an open SSE stream can keep uvicorn's graceful
        # shutdown waiting forever, which (with no pytest-level timeout) hangs
        # the whole job until CI cancels it. Cap the wait, then force-cancel.
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with suppress(asyncio.CancelledError):
                _ = await task  # assign-to-_ dodges CodeQL py/ineffectual-statement


@pytest.mark.asyncio
async def test_sse_client_roundtrip_against_fastmcp() -> None:
    server = _build_test_server()
    app = server.sse_app()
    port = _free_port()
    async with _run_uvicorn(app, port):
        cfg = MCPServerConfig(
            name="e2e-sse",
            transport="sse",
            url=f"http://127.0.0.1:{port}/sse",
            timeout_s=10.0,
        )
        client = SseMCPClient(config=cfg)
        await client.start()
        try:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert {"echo", "add"} <= tool_names

            echo_result = await client.call_tool("echo", {"text": "hello-sse"})
            assert "hello-sse" in echo_result.content
            assert echo_result.is_error is False

            add_result = await client.call_tool("add", {"a": 2, "b": 3})
            assert "5" in add_result.content
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_streamable_http_client_roundtrip_against_fastmcp() -> None:
    server = _build_test_server()
    app = server.streamable_http_app()
    port = _free_port()
    async with _run_uvicorn(app, port):
        cfg = MCPServerConfig(
            name="e2e-shttp",
            transport="streamable_http",
            url=f"http://127.0.0.1:{port}/mcp",
            timeout_s=10.0,
        )
        client = StreamableHttpMCPClient(config=cfg)
        await client.start()
        try:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert {"echo", "add"} <= tool_names

            echo_result = await client.call_tool("echo", {"text": "hello-shttp"})
            assert "hello-shttp" in echo_result.content

            add_result = await client.call_tool("add", {"a": 41, "b": 1})
            assert "42" in add_result.content
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_remote_client_timeout_raises_mcp_call_timeout_error() -> None:
    """Mini-ADR U-13: long-running tool exceeding ``timeout_s`` surfaces
    as :class:`MCPCallTimeoutError`, not a generic asyncio error."""
    server = FastMCP(name="expert-work-e2e-slow")

    @server.tool()
    async def slow() -> str:
        await asyncio.sleep(2.0)
        return "should never see this"

    app = server.streamable_http_app()
    port = _free_port()
    async with _run_uvicorn(app, port):
        # ``timeout_s`` feeds two different clocks: the httpx client-wide
        # timeout, baked in when ``start()`` builds the client, and the
        # per-call ``asyncio.wait_for`` cap, read fresh on every
        # ``call_tool``. Constructing with the tight value puts the MCP
        # ``initialize`` handshake on the same 100ms budget — a handshake
        # that loses that race fails the test in ``start()`` with
        # ``httpx.ReadTimeout``, never reaching the assertion below. Give
        # ``start()`` a sane budget, then tighten only the call cap.
        cfg = MCPServerConfig(
            name="e2e-slow",
            transport="streamable_http",
            url=f"http://127.0.0.1:{port}/mcp",
            timeout_s=5.0,
        )
        client = StreamableHttpMCPClient(config=cfg)
        await client.start()
        client.config = replace(cfg, timeout_s=0.1)  # 100ms — bites the 2s sleep
        try:
            from orchestrator.tools.mcp import MCPCallTimeoutError

            with pytest.raises(MCPCallTimeoutError, match="timed out"):
                await client.call_tool("slow", {})
        finally:
            await client.close()
