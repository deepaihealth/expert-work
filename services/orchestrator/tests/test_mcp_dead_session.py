"""B-177 — a pooled remote MCP client notices a dead session instead of hanging.

Production symptom (2026-10-12): the first "publish" click after the MCP peer
dropped its connection hung with no log line; the second click worked. A local
repro against the real StreamableHTTP transport showed three failure modes,
each pinned here:

- the transport dies under a request (peer reset): the SDK never answers the
  request, which used to wait out the full ``timeout_s``;
- the peer restarted and forgot the session (HTTP 404 "Session terminated"):
  every call failed at once until the pool's 30-minute expiry;
- the connection is silently dropped (half-open): each call waited out
  ``timeout_s`` and the session was never replaced.

The client here is a real ``ClientSession`` over anyio memory streams talking
to an in-process FastMCP server. "Transport died" is modelled the way the SDK
fails: a task in the transport's task group raises, which cancels the session's
owner task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

import orchestrator.tools.mcp as mcp_mod

_SERVER_TASKS: set[asyncio.Task[None]] = set()
#: Every ``echo`` / ``gated`` the in-process server actually started (reset per test).
_ECHO_CALLS: list[str] = []
_GATED_CALLS: list[str] = []
#: ``gated(key)`` runs until ``_GATES[key]`` is set.
_GATES: dict[str, asyncio.Event] = {}


def _fastmcp() -> FastMCP:
    srv = FastMCP("t")

    @srv.tool()
    def echo(text: str) -> str:
        _ECHO_CALLS.append(text)
        return text

    @srv.tool()
    async def gated(key: str) -> str:
        _GATED_CALLS.append(key)
        await _GATES.setdefault(key, asyncio.Event()).wait()
        return f"opened {key}"

    return srv


@dataclass
class _FlakyClient(mcp_mod._RemoteMCPClientBase):
    """Real ``ClientSession`` over memory streams; counts transport opens/closes.

    ``peer_resets[i]``: set it to make the i-th transport die (a task in its
    task group raises ``ConnectionResetError``, as the SDK's POST handler does
    on a reset connection). ``mode="silent"``: the server never answers
    (``initialize`` hangs). ``open_gate``: opening a transport waits on it.
    """

    _server: Any = field(default_factory=_fastmcp, init=False, repr=False)
    opens: int = field(default=0, init=False)
    closes: int = field(default=0, init=False)
    peer_resets: list[asyncio.Event] = field(default_factory=list, init=False)
    mode: Literal["serve", "silent"] = field(default="serve", init=False)
    open_gate: asyncio.Event | None = field(default=None, init=False)

    async def _open_streams(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        self.opens += 1
        if self.open_gate is not None:
            await self.open_gate.wait()
        c2s_send, c2s_recv = anyio.create_memory_object_stream[Any](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[Any](16)

        def _closed() -> None:
            self.closes += 1
            c2s_send.close()
            s2c_recv.close()

        stack.callback(_closed)
        if self.mode == "serve":
            low = self._server._mcp_server
            task = asyncio.create_task(
                low.run(c2s_recv, s2c_send, low.create_initialization_options())
            )
            _SERVER_TASKS.add(task)
            task.add_done_callback(_SERVER_TASKS.discard)
        reset = asyncio.Event()
        self.peer_resets.append(reset)

        async def _post_handler() -> None:
            await reset.wait()
            raise ConnectionResetError("peer reset the connection")

        tg = await stack.enter_async_context(anyio.create_task_group())
        tg.start_soon(_post_handler)
        stack.callback(tg.cancel_scope.cancel)  # a normal close stops it first
        return s2c_recv, c2s_send


def _client(timeout_s: float = 10.0) -> _FlakyClient:
    return _FlakyClient(
        config=mcp_mod.MCPServerConfig(
            name="deep-ai-health-mcp",
            transport="streamable_http",
            url="http://peer.test/mcp?token=secret",
            timeout_s=timeout_s,
        )
    )


class _Recording:
    """Delegates to ``inner``; counts the requests sent on this session."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls += 1
        return await self.inner.call_tool(name, args)

    async def list_tools(self) -> Any:
        self.calls += 1
        return await self.inner.list_tools()


class _Forgotten:
    """A session the server no longer knows: what the SDK raises on HTTP 404."""

    def __init__(self, code: int = 32600, message: str = "Session terminated") -> None:
        self.error = ErrorData(code=code, message=message)
        self.calls = 0

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls += 1
        raise McpError(self.error)

    async def list_tools(self) -> Any:
        self.calls += 1
        raise McpError(self.error)


class _SpyBreaker:
    def __init__(self) -> None:
        self.events: list[str] = []

    def allow_call(self) -> bool:
        return True

    def record_success(self) -> None:
        self.events.append("success")

    def record_failure(self) -> None:
        self.events.append("failure")


@pytest.fixture(autouse=True)
async def _stop_servers() -> AsyncIterator[None]:
    _ECHO_CALLS.clear()
    _GATED_CALLS.clear()
    _GATES.clear()
    yield
    for gate in _GATES.values():
        gate.set()
    for task in list(_SERVER_TASKS):
        task.cancel()
    await asyncio.gather(*_SERVER_TASKS, return_exceptions=True)


@pytest.fixture
def mcp_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    results: list[str] = []

    def _record(*, transport: str, server: str, result: str) -> None:
        results.append(result)

    monkeypatch.setattr(mcp_mod, "record_mcp_call", _record)
    return results


async def _started_gated_call(client: _FlakyClient, key: str) -> asyncio.Task[Any]:
    task = asyncio.create_task(client.call_tool("gated", {"key": key}))
    while key not in _GATED_CALLS:
        await asyncio.sleep(0.01)
    return task


async def _drained(client: _FlakyClient) -> None:
    await asyncio.gather(*client._drain_closes)


def _messages(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if name in r.getMessage()]


# ---------------------------------------------------------------------------
# ① the transport dies under a request
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_transport_death_fails_the_waiting_call_at_once(
    mcp_calls: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The peer resets the connection while ``gated`` runs. The SDK never
    answers that request; the call must fail now, not after ``timeout_s`` (10 s
    here, the outer cap is 5 s), and must not be resent — the server already
    started it."""
    client = _client(timeout_s=10.0)
    await client.start()
    spy = _SpyBreaker()
    client._breaker = spy  # type: ignore[assignment]
    task = await _started_gated_call(client, "a")

    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        client.peer_resets[0].set()
        with pytest.raises(mcp_mod.MCPSessionBrokenError, match="ConnectionResetError"):
            _ = await asyncio.wait_for(task, timeout=5)

    assert _GATED_CALLS == ["a"]  # not resent
    assert spy.events == ["failure"]
    assert mcp_calls == ["transport_err"]
    assert client.session_broken is True
    broken = _messages(caplog, "mcp.session_marked_broken")
    assert broken == [
        "mcp.session_marked_broken server=deep-ai-health-mcp "
        "transport=streamable_http err=ConnectionResetError"
    ]
    await client.close()


@pytest.mark.timeout(30)
async def test_next_call_after_transport_death_reconnects_before_sending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    await client.start()
    dead = _Recording(client._session)
    client._session = dead
    task = await _started_gated_call(client, "a")
    client.peer_resets[0].set()
    with pytest.raises(mcp_mod.MCPSessionBrokenError):
        _ = await asyncio.wait_for(task, timeout=5)

    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        result = await asyncio.wait_for(client.call_tool("echo", {"text": "x"}), timeout=5)

    assert result.content == "x"
    assert dead.calls == 1  # only "a": nothing more went out on the dead session
    assert (client.opens, client.closes) == (2, 1)
    assert client.session_broken is False
    assert len(_messages(caplog, "mcp.session_restarted")) == 1
    await client.close()


# ---------------------------------------------------------------------------
# ② the peer forgot the session (HTTP 404 "Session terminated")
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_session_terminated_reconnects_and_resends_once(
    mcp_calls: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """404 = the server did not run the request: reconnect and resend it once.
    One logical call, one breaker success."""
    client = _client()
    await client.start()
    spy = _SpyBreaker()
    client._breaker = spy  # type: ignore[assignment]
    forgotten = _Forgotten()
    client._session = forgotten

    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        result = await client.call_tool("echo", {"text": "once"})

    assert result.content == "once"
    assert forgotten.calls == 1
    assert _ECHO_CALLS == ["once"]  # executed exactly once, on the fresh session
    assert client.opens == 2
    assert spy.events == ["success"]
    assert mcp_calls == ["ok"]
    assert _messages(caplog, "mcp.session_terminated_resend") == [
        "mcp.session_terminated_resend server=deep-ai-health-mcp transport=streamable_http"
    ]
    await client.close()


@pytest.mark.timeout(30)
async def test_session_terminated_list_tools_reconnects_and_retries() -> None:
    client = _client()
    await client.start()
    client._session = _Forgotten()

    tools = await client.list_tools()

    assert sorted(t.name for t in tools) == ["echo", "gated"]
    assert client.opens == 2
    await client.close()


@pytest.mark.timeout(30)
async def test_session_terminated_again_after_reconnect_is_not_retried_twice(
    mcp_calls: list[str],
) -> None:
    client = _client()
    await client.start()
    client._session = _Forgotten()
    still_forgotten = _Forgotten()
    real_open = client._open_session

    async def _open_forgetful() -> tuple[Any, Any]:
        stack, _session = await real_open()
        return stack, still_forgotten

    client._open_session = _open_forgetful  # type: ignore[method-assign]

    with pytest.raises(McpError, match="Session terminated"):
        await client.call_tool("echo", {"text": "x"})

    assert still_forgotten.calls == 1
    assert client.opens == 2  # one reconnect, no loop
    assert mcp_calls == ["transport_err"]
    await client.close()


@pytest.mark.timeout(30)
async def test_other_server_errors_are_not_resent() -> None:
    """Only the 404 marker proves the server skipped the request."""
    client = _client()
    await client.start()
    failing = _Forgotten(code=-32603, message="Internal error")
    client._session = failing

    with pytest.raises(McpError, match="Internal error"):
        await client.call_tool("echo", {"text": "x"})

    assert failing.calls == 1
    assert client.opens == 1
    await client.close()


# ---------------------------------------------------------------------------
# ④ a request times out (half-open connection)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_timeout_rebuilds_the_session_in_the_background_without_resending(
    mcp_calls: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(timeout_s=0.3)
    await client.start()
    old = client._session

    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        with pytest.raises(mcp_mod.MCPCallTimeoutError):
            await client.call_tool("gated", {"key": "a"})
        assert client._rebuild is not None
        await asyncio.wait_for(client._rebuild, timeout=5)

    assert _GATED_CALLS == ["a"]  # the timed-out request was not resent
    assert mcp_calls == ["timeout"]
    assert client.opens == 2
    assert client._session is not old
    assert client.session_broken is False
    await _drained(client)
    assert client.closes == 1  # the stale session was closed
    assert _messages(caplog, "mcp.session_rebuild_scheduled") == [
        "mcp.session_rebuild_scheduled server=deep-ai-health-mcp "
        "transport=streamable_http reason=timeout"
    ]
    result = await client.call_tool("echo", {"text": "next"})
    assert result.content == "next"
    assert client.opens == 2
    await client.close()


@pytest.mark.timeout(30)
async def test_next_call_after_a_timeout_never_goes_out_on_the_stale_session() -> None:
    """The next call is issued while the rebuild is still connecting: it waits
    for the new session instead of sending on the suspect one."""
    client = _client(timeout_s=0.3)
    await client.start()
    stale = _Recording(client._session)
    client._session = stale
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await client.call_tool("gated", {"key": "a"})
    assert client.session_broken is True
    client.open_gate = asyncio.Event()  # the rebuild's handshake is held

    nxt = asyncio.create_task(client.call_tool("echo", {"text": "next"}))
    while client.opens < 2:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert stale.calls == 1  # only "a"
    assert not nxt.done()

    client.open_gate.set()
    result = await asyncio.wait_for(nxt, timeout=5)
    assert result.content == "next"
    assert stale.calls == 1
    assert client.opens == 2  # the call shared the background rebuild
    await client.close()


@pytest.mark.timeout(30)
async def test_rebuild_lets_calls_still_running_on_the_old_session_finish() -> None:
    """One call timing out must not tear the session down under another call
    that is still running fine on it (a shared pooled client)."""
    client = _client(timeout_s=10.0)
    await client.start()
    running = await _started_gated_call(client, "b")  # deadline 10 s
    client.config = replace(client.config, timeout_s=0.3)

    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await client.call_tool("gated", {"key": "a"})
    assert client._rebuild is not None
    await asyncio.wait_for(client._rebuild, timeout=5)
    assert (client.opens, client.closes) == (2, 0)  # old one parked for "b"

    _GATES["b"].set()
    result = await asyncio.wait_for(running, timeout=5)
    assert result.content == "opened b"
    await _drained(client)
    assert (client.opens, client.closes) == (2, 1)  # the last call out closed it
    await client.close()
    assert client.closes == 2


@pytest.mark.timeout(30)
async def test_close_during_a_background_rebuild_is_a_hard_stop() -> None:
    client = _client(timeout_s=0.3)
    await client.start()
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await client.call_tool("gated", {"key": "a"})
    client.open_gate = asyncio.Event()
    while client.opens < 2:
        await asyncio.sleep(0.01)

    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    client.open_gate.set()
    await asyncio.wait_for(closing, timeout=5)
    assert client._rebuild is not None
    await asyncio.wait_for(client._rebuild, timeout=5)

    assert client.opens == client.closes == 2
    with pytest.raises(RuntimeError, match="not started"):
        await client.call_tool("echo", {"text": "x"})


# ---------------------------------------------------------------------------
# ③ connect / reconnect are bounded by timeout_s
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_start_against_a_silent_server_is_bounded_by_timeout_s() -> None:
    client = _client(timeout_s=0.3)
    client.mode = "silent"

    started = time.monotonic()
    with pytest.raises(mcp_mod.MCPCallTimeoutError, match="connect timed out"):
        await asyncio.wait_for(client.start(), timeout=5)

    assert time.monotonic() - started < 3.0
    assert (client.opens, client.closes) == (1, 1)


@pytest.mark.timeout(30)
async def test_reconnect_against_a_silent_server_is_bounded_and_retried_later() -> None:
    client = _client(timeout_s=0.3)
    await client.start()
    task = await _started_gated_call(client, "a")
    client.peer_resets[0].set()
    with pytest.raises(mcp_mod.MCPSessionBrokenError):
        _ = await asyncio.wait_for(task, timeout=5)
    client.mode = "silent"  # the peer accepts connections but never answers

    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await asyncio.wait_for(client.call_tool("echo", {"text": "x"}), timeout=5)

    client.mode = "serve"
    result = await asyncio.wait_for(client.call_tool("echo", {"text": "y"}), timeout=5)
    assert result.content == "y"
    assert client.opens == 3
    await client.close()
    assert client.opens == client.closes
