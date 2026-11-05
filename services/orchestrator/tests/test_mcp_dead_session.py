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


def _broken(client: mcp_mod._RemoteMCPClientBase) -> bool:
    """The next call will reconnect before sending."""
    stack = client._stack
    return isinstance(stack, mcp_mod._OwnedSession) and stack.broken


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
    assert _broken(client)
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
    assert not _broken(client)
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
@pytest.mark.parametrize(
    ("code", "message"),
    [(-32603, "Internal error"), (-32600, "Session terminated")],
    ids=["internal-error", "negative-code"],
)
async def test_other_server_errors_are_not_resent(code: int, message: str) -> None:
    """Only the SDK's exact 404 marker (+32600) proves the server skipped the
    request; anything else may have run on the server."""
    client = _client()
    await client.start()
    failing = _Forgotten(code=code, message=message)
    client._session = failing

    with pytest.raises(McpError, match=message):
        await client.call_tool("echo", {"text": "x"})

    assert failing.calls == 1
    assert client.opens == 1
    await client.close()


class _ForgetsEcho:
    """The live session, except that ``echo`` meets an HTTP 404."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "echo":
            raise McpError(ErrorData(code=32600, message="Session terminated"))
        return await self.inner.call_tool(name, args)

    async def list_tools(self) -> Any:
        return await self.inner.list_tools()


@pytest.mark.timeout(30)
async def test_session_terminated_lets_another_call_in_flight_finish() -> None:
    """Review: the 404 reconnect used to tear the shared session down under a
    call still running on it — the server ran ``B`` but the client reported a
    timeout. The old session now drains first."""
    client = _client(timeout_s=2.0)
    await client.start()
    running = await _started_gated_call(client, "B")
    client._session = _ForgetsEcho(client._session)

    result = await client.call_tool("echo", {"text": "a"})
    assert result.content == "a"
    assert (client.opens, client.closes) == (2, 0)  # old one parked for "B"

    _GATES["B"].set()
    assert (await asyncio.wait_for(running, timeout=5)).content == "opened B"
    await _drained(client)
    assert (client.opens, client.closes) == (2, 1)
    await client.close()


@pytest.mark.timeout(30)
async def test_list_tools_retries_once_when_the_transport_dies_under_it() -> None:
    """list_tools is read-only: unlike call_tool, a transport death mid-request
    is retried once on a fresh session."""
    client = _client()
    await client.start()
    live = client._session
    entered = asyncio.Event()

    class _Unanswered:
        async def list_tools(self) -> Any:
            entered.set()
            await asyncio.Event().wait()  # the response never arrives

    client._session = _Unanswered()
    listing = asyncio.create_task(client.list_tools())
    await entered.wait()
    client.peer_resets[0].set()

    tools = await asyncio.wait_for(listing, timeout=5)
    assert sorted(t.name for t in tools) == ["echo", "gated"]
    assert client.opens == 2
    assert client._session is not live
    await client.close()


@pytest.mark.timeout(30)
async def test_owner_cancelled_at_shutdown_is_not_reported_as_broken(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Loop shutdown cancels the owner task: not a dead peer, no WARNING."""
    client = _client()
    await client.start()
    stack = client._stack
    assert isinstance(stack, mcp_mod._OwnedSession)

    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        stack._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stack._task
        await asyncio.sleep(0)

    assert _messages(caplog, "mcp.session_marked_broken") == []
    assert not stack.died.done()


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
    assert not _broken(client)
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
    assert _broken(client)
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


@pytest.mark.timeout(60)
async def test_callers_queued_behind_a_failed_reconnect_fail_together(
    mcp_calls: list[str],
) -> None:
    """Review: the peer stopped answering. Callers queued behind the
    background rebuild used to each dial it again in turn (6 callers →
    6 x timeout_s, 8 connects); now the rebuild's failure is theirs too."""
    client = _client(timeout_s=0.5)
    await client.start()
    client._breaker = _SpyBreaker()  # type: ignore[assignment]  # isolate from the breaker
    client.mode = "silent"  # from now on the peer never answers ``initialize``
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await client.call_tool("gated", {"key": "a"})  # → background rebuild

    results = await asyncio.wait_for(
        asyncio.gather(
            *(client.call_tool("echo", {"text": str(i)}) for i in range(6)),
            return_exceptions=True,
        ),
        timeout=10,
    )

    assert [type(r).__name__ for r in results] == ["MCPCallTimeoutError"] * 6
    # Exactly one dial (the background rebuild, or a caller that got the lock
    # before it ran); everyone queued behind it fails with its outcome.
    assert client.opens == 2
    assert sum("reconnect failed moments ago" in str(r) for r in results) >= 5
    assert mcp_calls == ["timeout"] * 7

    client.mode = "serve"  # a later caller tries again and heals
    result = await asyncio.wait_for(client.call_tool("echo", {"text": "later"}), timeout=5)
    assert result.content == "later"
    assert client.opens == 3
    await client.close()


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


# ---------------------------------------------------------------------------
# a replaced session is closed without ending it on the server (no DELETE)
# ---------------------------------------------------------------------------


class _FakeHttpTransport:
    """Stands in for the SDK's ``StreamableHTTPTransport``: records DELETEs."""

    def __init__(self) -> None:
        self.session_id = f"sess-{len(_HTTP_TRANSPORTS)}"
        self.deletes = 0

    def get_session_id(self) -> str | None:
        return self.session_id

    async def terminate_session(self, client: Any) -> None:
        if _HTTP_FAULTS.get("terminate") is not None:
            raise _HTTP_FAULTS["terminate"]
        self.deletes += 1


_HTTP_TRANSPORTS: list[_FakeHttpTransport] = []
#: ``"open"``: the next transports fail to connect; ``"terminate"``: DELETE raises.
_HTTP_FAULTS: dict[str, BaseException] = {}


@contextlib.asynccontextmanager
async def _fake_streamable_http_client(
    url: str, *, http_client: Any = None, terminate_on_close: bool = True
) -> AsyncIterator[tuple[Any, Any, Any]]:
    """The SDK helper's contract over memory streams to an in-process server."""
    if _HTTP_FAULTS.get("open") is not None:
        raise _HTTP_FAULTS["open"]
    transport = _FakeHttpTransport()
    _HTTP_TRANSPORTS.append(transport)
    c2s_send, c2s_recv = anyio.create_memory_object_stream[Any](16)
    s2c_send, s2c_recv = anyio.create_memory_object_stream[Any](16)
    low = _fastmcp()._mcp_server
    task = asyncio.create_task(low.run(c2s_recv, s2c_send, low.create_initialization_options()))
    _SERVER_TASKS.add(task)
    task.add_done_callback(_SERVER_TASKS.discard)
    try:
        yield s2c_recv, c2s_send, transport.get_session_id
    finally:
        if terminate_on_close:
            await transport.terminate_session(http_client)
        c2s_send.close()
        s2c_recv.close()


def _http_client(monkeypatch: pytest.MonkeyPatch) -> mcp_mod.StreamableHttpMCPClient:
    """The production StreamableHTTP client over the in-memory SDK stand-in."""
    import mcp.client.streamable_http as sdk_http

    _HTTP_TRANSPORTS.clear()
    _HTTP_FAULTS.clear()
    monkeypatch.setattr(sdk_http, "streamable_http_client", _fake_streamable_http_client)
    return mcp_mod.StreamableHttpMCPClient(
        config=mcp_mod.MCPServerConfig(
            name="deep-ai-health-mcp",
            transport="streamable_http",
            url="http://peer.test/mcp",
            timeout_s=0.3,
        )
    )


@pytest.mark.timeout(30)
@pytest.mark.parametrize("replaced_by", ["timeout", "session_terminated"])
async def test_replaced_session_is_closed_without_ending_it_on_the_server(
    monkeypatch: pytest.MonkeyPatch, replaced_by: str
) -> None:
    """Review: after a timeout the server may still be running that tool; a
    DELETE on the replaced session could abort it. Only an explicit close ends
    the server-side session, as before."""
    client = _http_client(monkeypatch)
    await client.start()
    if replaced_by == "timeout":
        with pytest.raises(mcp_mod.MCPCallTimeoutError):
            await client.call_tool("gated", {"key": "slow"})
        assert client._rebuild is not None
        await asyncio.wait_for(client._rebuild, timeout=5)
    else:
        client._session = _ForgetsEcho(client._session)
        assert (await client.call_tool("echo", {"text": "x"})).content == "x"
    await asyncio.gather(*client._drain_closes)
    old, new = _HTTP_TRANSPORTS

    assert old.deletes == 0  # replaced: closed, the server-side session left alone
    await client.close()
    assert new.deletes == 1  # an explicit close still ends it


@pytest.mark.timeout(30)
@pytest.mark.parametrize("release", ["close", "retire"])
async def test_explicit_release_of_a_stale_current_session_still_ends_it(
    monkeypatch: pytest.MonkeyPatch, release: str
) -> None:
    """A request timed out and the rebuild failed: the stale session is still
    the current one. Closing / retiring it is an explicit release, not a
    replacement, so the server-side session is ended as before."""
    client = _http_client(monkeypatch)
    await client.start()
    _HTTP_FAULTS["open"] = ConnectionError("peer down")
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await client.call_tool("gated", {"key": "slow"})
    assert client._rebuild is not None
    await asyncio.wait_for(client._rebuild, timeout=5)  # failed: still stale and current
    assert _broken(client)

    await getattr(client, release)()

    (only,) = _HTTP_TRANSPORTS
    assert only.deletes == 1


@pytest.mark.timeout(30)
async def test_a_failing_server_session_end_is_logged_and_close_completes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _http_client(monkeypatch)
    await client.start()
    _HTTP_FAULTS["terminate"] = TypeError("terminate_session() takes 1 positional argument")

    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        await asyncio.wait_for(client.close(), timeout=5)

    assert _messages(caplog, "mcp.session_terminate_failed") == [
        "mcp.session_terminate_failed server=deep-ai-health-mcp err=TypeError"
    ]


async def test_sdk_transport_still_offers_the_session_end_we_call() -> None:
    """Contract with the mcp SDK (re-verify on upgrade): the session-id callback
    the transport yields is bound to the transport, and its
    ``terminate_session(httpx.AsyncClient)`` sends the session DELETE."""
    import httpx
    from mcp.client.streamable_http import StreamableHTTPTransport

    seen: list[tuple[str, str | None]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("mcp-session-id")))
        return httpx.Response(200)

    transport = StreamableHTTPTransport("http://peer.test/mcp")
    transport.session_id = "sess-1"  # type: ignore[assignment]  # set by initialize normally
    bound = getattr(transport.get_session_id, "__self__", None)  # the lookup mcp.py does
    assert bound is transport
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http_client:
        await bound.terminate_session(http_client)

    assert seen == [("DELETE", "sess-1")]
