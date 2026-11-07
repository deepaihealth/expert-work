"""B-294 —— 连上过的 MCP 服务掉线后,构建跳过它,不再让整个 run 500。

现场(测试集群):智能体的 MCP 服务先连上、后掉线。已经连上过的副本上,之后每次
构建都在 ``register_mcp_tools`` 的 ``list_tools()`` 上抛连接错误(重连也连不上),
``build_tool_registry`` 整个失败 → run 入口 HTTP 500,直到池 TTL(30 分钟)。

修法:只有「连不上 / 会话断了」这一类错误跳过这台服务(本次构建没有它的工具,
run 照常),其它错误(编程错误、鉴权 4xx、取消)照旧抛出。跳过的服务记在 registry
上(``skipped_mcp_servers``),控制面据此不缓存这次构建;服务恢复后下一次构建由
客户端自己重连(B-177),工具回来。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
import pytest
from mcp.server.fastmcp import FastMCP

import orchestrator.tools.mcp as mcp_mod
from expert_work.protocol import ArgBindingSpec, MCPToolSpec
from orchestrator.tools import (
    MCPServerPool,
    MCPToolDef,
    RecordingMCPClient,
    ToolEnv,
    build_tool_registry,
)

_URL = "http://peer.test/mcp?token=secret-token"
_REQ = httpx.Request("POST", _URL)


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("All connection attempts failed", request=_REQ)


def _status_error(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"Server error '{code}' for url {_URL!r}",
        request=_REQ,
        response=httpx.Response(code, request=_REQ),
    )


@dataclass
class _DownClient(RecordingMCPClient):
    """``list_tools`` 抛 ``error``(``None`` = 正常返回 ``tools``)。"""

    error: BaseException | None = None

    async def list_tools(self) -> Any:
        if self.error is not None:
            raise self.error
        return await super().list_tools()


def _tool(name: str) -> MCPToolDef:
    return MCPToolDef(name=name, description="", input_schema={})


_POOLS = ["mcp_pool", "platform_mcp_pool", "tenant_mcp_pool", "user_mcp_oauth_pool"]


async def _env(pool_field: str, clients: dict[str, Any]) -> ToolEnv:
    pool = MCPServerPool()
    for name, client in clients.items():
        await pool.add(name, client)
    # 平台两个池按 allowlist 放行(共享目录池空表 = 一个不放),这里全放。
    fields: dict[str, Any] = {pool_field: pool, "mcp_allowlist": tuple(clients)}
    return ToolEnv(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_field", _POOLS)
async def test_a_server_whose_list_tools_fails_to_connect_is_skipped(pool_field: str) -> None:
    """现场形态:SDK 把 httpx 的 ConnectError 包在 TaskGroup 的 ExceptionGroup 里。
    四个池的循环都不再抛,同池其它服务的工具照常注册。"""
    down = _DownClient(tools=(_tool("echo"),), error=ExceptionGroup("tg", [_connect_error()]))
    up = RecordingMCPClient(tools=(_tool("ping"),))
    env = await _env(pool_field, {"down": down, "up": up})

    registry = await build_tool_registry([MCPToolSpec()], tool_env=env)

    assert registry.get("mcp__down__echo") is None
    assert registry.get("mcp__up__ping") is not None
    assert registry.skipped_mcp_servers() == ("down",)


@pytest.mark.asyncio
async def test_a_healthy_build_records_no_skipped_server() -> None:
    env = await _env("tenant_mcp_pool", {"up": RecordingMCPClient(tools=(_tool("ping"),))})
    registry = await build_tool_registry([MCPToolSpec()], tool_env=env)
    assert registry.skipped_mcp_servers() == ()


@pytest.mark.asyncio
async def test_two_mcp_entries_record_the_same_skipped_server_once() -> None:
    env = await _env("tenant_mcp_pool", {"down": _DownClient(error=_connect_error())})
    registry = await build_tool_registry(
        [MCPToolSpec(allow_tools=["a"]), MCPToolSpec(allow_tools=["b"])], tool_env=env
    )
    assert registry.skipped_mcp_servers() == ("down",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        mcp_mod.MCPSessionBrokenError("connection lost"),
        mcp_mod.MCPCallTimeoutError("mcp connect timed out"),
        TimeoutError(),
        _connect_error(),
        httpx.RemoteProtocolError("Server disconnected", request=_REQ),
        httpx.ReadTimeout("read timed out", request=_REQ),
        _status_error(502),
        _status_error(503),
        anyio.BrokenResourceError(),
        anyio.ClosedResourceError(),
        ConnectionResetError("peer reset"),
        ExceptionGroup("tg", [_status_error(502)]),
        ExceptionGroup("outer", [ExceptionGroup("tg", [_connect_error()])]),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_connection_style_errors_skip_the_server(error: BaseException) -> None:
    env = await _env("tenant_mcp_pool", {"down": _DownClient(error=error)})
    registry = await build_tool_registry([MCPToolSpec()], tool_env=env)
    assert registry.skipped_mcp_servers() == ("down",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("a bug"),
        KeyError("a bug"),
        RuntimeError("StreamableHttpMCPClient 'down' not started"),
        _status_error(401),
        _status_error(403),
        # 一组里只要混着一个不是连接错误的,就不是「连不上」:照旧抛。
        ExceptionGroup("tg", [_connect_error(), ValueError("a bug")]),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_other_errors_still_fail_the_build(error: BaseException) -> None:
    env = await _env("tenant_mcp_pool", {"down": _DownClient(error=error)})
    with pytest.raises(type(error)):
        await build_tool_registry([MCPToolSpec()], tool_env=env)


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    env = await _env("tenant_mcp_pool", {"down": _DownClient(error=asyncio.CancelledError())})
    with pytest.raises(asyncio.CancelledError):
        await build_tool_registry([MCPToolSpec()], tool_env=env)


@pytest.mark.asyncio
async def test_the_skip_is_logged_with_server_name_and_error_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    down = _DownClient(error=ExceptionGroup("tg", [_connect_error()]))
    env = await _env("tenant_mcp_pool", {"down": down})
    with caplog.at_level(logging.WARNING):
        await build_tool_registry([MCPToolSpec()], tool_env=env)
    skipped = [r for r in caplog.records if "mcp.server_unreachable_skipped" in r.getMessage()]
    assert len(skipped) == 1
    text = skipped[0].getMessage()
    assert "server=down" in text
    assert "err=ConnectError" in text
    assert "peer.test" not in text
    assert "secret-token" not in text
    assert "All connection attempts failed" not in text


@pytest.mark.asyncio
async def test_a_skipped_platform_server_still_reserves_its_name() -> None:
    """平台服务掉线时,同名的租户服务不能借机顶上来(名字照旧由平台占着)。"""
    platform = MCPServerPool()
    await platform.add("shared", _DownClient(error=_connect_error()))
    tenant = MCPServerPool()
    await tenant.add("shared", RecordingMCPClient(tools=(_tool("from_tenant"),)))
    registry = await build_tool_registry(
        [MCPToolSpec()], tool_env=ToolEnv(mcp_pool=platform, tenant_mcp_pool=tenant)
    )
    assert registry.get("mcp__shared__from_tenant") is None
    assert registry.skipped_mcp_servers() == ("shared",)


@pytest.mark.asyncio
async def test_a_binding_on_a_skipped_server_is_not_reported_as_tool_missing() -> None:
    """服务连不上不等于「目录里没这个工具」:不能对配置的人报一条假的落空。"""
    env = await _env("tenant_mcp_pool", {"down": _DownClient(error=_connect_error())})
    binding = ArgBindingSpec(server="down", tool="echo", args={"text": "customer_id"})
    registry = await build_tool_registry([MCPToolSpec(arg_bindings=[binding])], tool_env=env)
    assert registry.skipped_mcp_servers() == ("down",)
    assert registry.unmatched_arg_bindings() == ()


# ---------------------------------------------------------------------------
# 真客户端:连上 → 对端掉线 → 构建跳过 → 对端回来 → 下一次构建工具回来(B-177 自愈)
# ---------------------------------------------------------------------------

_SERVER_TASKS: set[asyncio.Task[None]] = set()


def _fastmcp() -> FastMCP:
    srv = FastMCP("t")

    @srv.tool()
    def echo(text: str) -> str:
        return text

    return srv


@dataclass
class _PeerClient(mcp_mod._RemoteMCPClientBase):
    """真 ``ClientSession`` 走内存流连进程内 FastMCP。

    ``kill()``:当前传输死掉(传输 task group 里一个 task 抛 httpx.ConnectError,
    与 SDK 的 POST 处理在对端没了时一样)。``down=True``:新开传输连不上 ——
    同 SDK 的形态,ConnectError 包在 TaskGroup 的 ExceptionGroup 里。
    """

    _server: Any = field(default_factory=_fastmcp, init=False, repr=False)
    down: bool = field(default=False, init=False)
    opens: int = field(default=0, init=False)
    _kill: asyncio.Event | None = field(default=None, init=False, repr=False)

    def kill(self) -> None:
        assert self._kill is not None
        self._kill.set()

    async def _open_streams(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        self.opens += 1
        if self.down:

            async def _refused() -> None:
                raise _connect_error()

            async with anyio.create_task_group() as refusing:
                refusing.start_soon(_refused)
        c2s_send, c2s_recv = anyio.create_memory_object_stream[Any](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[Any](16)
        stack.callback(c2s_send.close)
        stack.callback(s2c_recv.close)
        low = self._server._mcp_server
        task = asyncio.create_task(low.run(c2s_recv, s2c_send, low.create_initialization_options()))
        _SERVER_TASKS.add(task)
        task.add_done_callback(_SERVER_TASKS.discard)
        kill = asyncio.Event()
        self._kill = kill

        async def _post_handler() -> None:
            await kill.wait()
            raise _connect_error()

        tg = await stack.enter_async_context(anyio.create_task_group())
        tg.start_soon(_post_handler)
        stack.callback(tg.cancel_scope.cancel)
        return s2c_recv, c2s_send


@pytest.mark.asyncio
async def test_a_real_client_recovers_on_the_next_build_once_the_server_is_back() -> None:
    client = _PeerClient(
        config=mcp_mod.MCPServerConfig(
            name="peer", transport="streamable_http", url=_URL, timeout_s=5.0
        )
    )
    await client.start()
    try:
        pool = MCPServerPool()
        await pool.add("peer", client)
        env = ToolEnv(tenant_mcp_pool=pool)
        before = await build_tool_registry([MCPToolSpec()], tool_env=env)
        assert before.get("mcp__peer__echo") is not None

        # 对端掉线:当前会话的传输死掉,重连也连不上。
        client.down = True
        client.kill()
        await asyncio.sleep(0)  # 让传输的死讯传到 ``died``
        during = await build_tool_registry([MCPToolSpec()], tool_env=env)
        assert during.get("mcp__peer__echo") is None
        assert during.skipped_mcp_servers() == ("peer",)
        opens_while_down = client.opens

        # 对端回来:同一个池里的同一个客户端,下一次构建自己重连,工具回来。
        client.down = False
        after = await build_tool_registry([MCPToolSpec()], tool_env=env)
        assert after.get("mcp__peer__echo") is not None
        assert after.skipped_mcp_servers() == ()
        assert client.opens == opens_while_down + 1
        assert pool.get("peer") is client
    finally:
        await client.close()
