"""A remote MCP client retired by its pool keeps serving the holders that still
reference it, through a one-shot session per call; a closed client stays a
hard stop.

Production symptom (2026-10-30, one replica): every call to a platform-catalog
MCP server failed with ``RuntimeError: StreamableHttpMCPClient '...' not
started``. The pools (platform / tenant / user-OAuth) closed every client on TTL
expiry, but cached ``BuiltAgent``s and in-flight runs still hold ``MCPTool``s
bound to those client objects. On TTL expiry the pools now ``retire()`` their
clients instead: each later call opens a fresh session under one ``timeout_s``
deadline, uses it, and closes it again. ``close()`` (invalidation: server
removed / disabled / disconnected, app shutdown) still makes later calls fail.

The client here is a real ``ClientSession`` over anyio memory streams talking to
an in-process FastMCP server, so the one-shot path runs the production
open / initialize / call / close sequence end to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
import pytest
from mcp.server.fastmcp import FastMCP

import orchestrator.tools.mcp as mcp_mod

_SERVER_TASKS: set[asyncio.Task[None]] = set()
#: Every ``echo`` the in-process server actually executed (reset per test).
_ECHO_CALLS: list[str] = []
#: B-248 — ``gated(key)`` runs until ``_GATES[key]`` is set; ``_GATED_CALLS``
#: records each key the server started on (both reset per test).
_GATES: dict[str, asyncio.Event] = {}
_GATED_CALLS: list[str] = []


def _fastmcp() -> FastMCP:
    srv = FastMCP("t")

    @srv.tool()
    def echo(text: str) -> str:
        _ECHO_CALLS.append(text)
        return text

    @srv.tool()
    async def slow() -> str:
        await asyncio.sleep(5)
        return "late"

    @srv.tool()
    async def gated(key: str) -> str:
        _GATED_CALLS.append(key)
        await _GATES.setdefault(key, asyncio.Event()).wait()
        return f"opened {key}"

    return srv


@dataclass
class _CountingClient(mcp_mod._RemoteMCPClientBase):
    """Real ``ClientSession`` over memory streams; counts transport opens/closes.

    ``mode``: ``"serve"`` answers through FastMCP; ``"silent"`` never answers
    (a server stuck before replying to ``initialize``); ``"hang_teardown"``
    serves normally but its transport teardown never finishes on its own.
    """

    _server: Any = field(default_factory=_fastmcp, init=False, repr=False)
    opens: int = field(default=0, init=False)
    closes: int = field(default=0, init=False)
    fail_open: BaseException | None = field(default=None, init=False)
    mode: Literal["serve", "silent", "hang_teardown"] = field(default="serve", init=False)
    #: When set, opening a transport waits on it (a handshake still in progress).
    open_gate: asyncio.Event | None = field(default=None, init=False)

    async def _open_streams(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        self.opens += 1
        if self.fail_open is not None:
            raise self.fail_open
        if self.open_gate is not None:
            await self.open_gate.wait()
        c2s_send, c2s_recv = anyio.create_memory_object_stream[Any](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[Any](16)

        def _closed() -> None:
            self.closes += 1
            c2s_send.close()
            s2c_recv.close()

        stack.callback(_closed)
        if self.mode != "silent":
            low = self._server._mcp_server
            task = asyncio.create_task(
                low.run(c2s_recv, s2c_send, low.create_initialization_options())
            )
            _SERVER_TASKS.add(task)
            task.add_done_callback(_SERVER_TASKS.discard)
        if self.mode == "hang_teardown":
            # Runs first on exit (LIFO), before ``_closed``.
            stack.push_async_callback(asyncio.sleep, 3600)
        return s2c_recv, c2s_send


def _client(timeout_s: float = 5.0) -> _CountingClient:
    return _CountingClient(
        config=mcp_mod.MCPServerConfig(
            name="deep-ai-health-mcp",
            transport="streamable_http",
            url="http://x/mcp",
            timeout_s=timeout_s,
        )
    )


async def _retired_client(timeout_s: float = 5.0) -> _CountingClient:
    client = _client(timeout_s)
    await client.start()
    await client.retire()
    assert (client.opens, client.closes) == (1, 1)
    return client


async def _drained(client: _CountingClient) -> None:
    """Wait for the background close of a drained session (B-248)."""
    if client._drain_close is not None:
        await client._drain_close


def _live_owner_tasks() -> list[asyncio.Task[Any]]:
    """Session owner tasks (``_open_owned_session.<locals>._own``) still running."""
    return [
        t
        for t in asyncio.all_tasks()
        if not t.done() and getattr(t.get_coro(), "__qualname__", "").endswith("._own")
    ]


@pytest.fixture(autouse=True)
async def _stop_servers() -> AsyncIterator[None]:
    _ECHO_CALLS.clear()
    _GATES.clear()
    _GATED_CALLS.clear()
    yield
    for task in list(_SERVER_TASKS):
        task.cancel()
    await asyncio.gather(*_SERVER_TASKS, return_exceptions=True)


class _SpyBreaker:
    """Duck-typed breaker capturing the exact record_* sequence."""

    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.events: list[str] = []

    def allow_call(self) -> bool:
        return self.allow

    def record_success(self) -> None:
        self.events.append("success")

    def record_failure(self) -> None:
        self.events.append("failure")


@pytest.fixture
def mcp_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    results: list[str] = []

    def _record(*, transport: str, server: str, result: str) -> None:
        results.append(result)

    monkeypatch.setattr(mcp_mod, "record_mcp_call", _record)
    return results


# ---------------------------------------------------------------------------
# retire() — later calls run on one-shot sessions
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_retired_client_call_tool_uses_a_one_shot_session(mcp_calls: list[str]) -> None:
    client = await _retired_client()

    result = await client.call_tool("echo", {"text": "still here"})
    second = await client.call_tool("echo", {"text": "again"})

    assert result.content == "still here"
    assert second.content == "again"
    # One fresh session per call, each closed before the call returns.
    assert (client.opens, client.closes) == (3, 3)
    assert mcp_calls == ["ok", "ok"]
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_retired_client_list_tools_uses_a_one_shot_session() -> None:
    client = await _retired_client()

    tools = await client.list_tools()
    await _drained(client)

    assert sorted(t.name for t in tools) == ["echo", "gated", "slow"]
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_retired_call_counts_success_on_the_breaker() -> None:
    client = await _retired_client()
    spy = _SpyBreaker()
    client._breaker = spy  # type: ignore[assignment]

    await client.call_tool("echo", {"text": "x"})

    assert spy.events == ["success"]


@pytest.mark.timeout(30)
async def test_retired_call_open_failure_counts_on_breaker_and_metrics(
    mcp_calls: list[str],
) -> None:
    client = await _retired_client()
    spy = _SpyBreaker()
    client._breaker = spy  # type: ignore[assignment]
    client.fail_open = ConnectionError("server down")

    with pytest.raises(ConnectionError):
        await client.call_tool("echo", {"text": "x"})

    assert spy.events == ["failure"]
    assert mcp_calls == ["transport_err"]


@pytest.mark.timeout(30)
async def test_retired_call_short_circuits_when_breaker_open(mcp_calls: list[str]) -> None:
    client = await _retired_client()
    client._breaker = _SpyBreaker(allow=False)  # type: ignore[assignment]

    with pytest.raises(mcp_mod.MCPServerUnhealthyError):
        await client.call_tool("echo", {"text": "x"})

    assert client.opens == 1  # only the original start — no round-trip
    assert mcp_calls == ["circuit_open"]


@pytest.mark.timeout(30)
async def test_retired_call_honors_timeout_and_closes_the_session(mcp_calls: list[str]) -> None:
    client = await _retired_client(timeout_s=0.2)
    spy = _SpyBreaker()
    client._breaker = spy  # type: ignore[assignment]

    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await client.call_tool("slow", {})

    assert spy.events == ["failure"]
    assert mcp_calls == ["timeout"]
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_retired_call_hung_initialize_times_out_within_timeout_s(
    mcp_calls: list[str],
) -> None:
    """Review M1: the server never answers ``initialize``. Open + initialize +
    call share one ``timeout_s`` deadline, so the call fails as a timeout in
    about ``timeout_s`` instead of hanging the run."""
    client = await _retired_client(timeout_s=1.0)
    spy = _SpyBreaker()
    client._breaker = spy  # type: ignore[assignment]
    client.mode = "silent"

    started = time.monotonic()
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        # The outer cap only keeps an unbounded handshake from hanging the suite.
        await asyncio.wait_for(client.call_tool("echo", {"text": "x"}), timeout=5)
    elapsed = time.monotonic() - started

    assert 0.9 <= elapsed < 3.0
    assert spy.events == ["failure"]
    assert mcp_calls == ["timeout"]
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_retired_list_tools_hung_initialize_times_out_within_timeout_s() -> None:
    client = await _retired_client(timeout_s=1.0)
    client.mode = "silent"

    started = time.monotonic()
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await asyncio.wait_for(client.list_tools(), timeout=5)

    assert time.monotonic() - started < 3.0
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_retired_teardown_hang_is_bounded_and_keeps_the_result(
    mcp_calls: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Review M1: a teardown that never finishes (e.g. a stuck session DELETE)
    gets its own short budget, is logged by type only, and never turns a
    successful call into an error."""
    client = await _retired_client(timeout_s=0.5)
    client.mode = "hang_teardown"

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        result = await asyncio.wait_for(client.call_tool("echo", {"text": "kept"}), timeout=5)

    assert result.content == "kept"
    assert time.monotonic() - started < 3.0
    assert mcp_calls == ["ok"]
    failed = [r for r in caplog.records if "mcp.retired_session_close_failed" in r.getMessage()]
    assert len(failed) == 1
    assert "err=TimeoutError" in failed[0].getMessage()
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_cancelled_retired_call_closes_its_session() -> None:
    """Review LOW-1: cancelling a run mid-call must still close the one-shot
    session — no transport or owner task may outlive the call."""
    client = await _retired_client(timeout_s=10)
    task = asyncio.create_task(client.call_tool("slow", {}))
    while client.opens < 2:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)  # initialize done, ``slow`` in flight

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task

    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_send_phase_death_racing_retire_resends_on_a_one_shot_session() -> None:
    """Review LOW-2: the call grabbed the live session, ``retire()`` tore it
    down, and the send failed with ``ClosedResourceError`` (send-phase, so the
    server never saw it). The resend goes out on a one-shot session instead of
    ending in "not started"."""
    client = _client()
    await client.start()
    spy = _SpyBreaker()
    client._breaker = spy  # type: ignore[assignment]

    class _RetiredUnderneath:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            await client.retire()
            raise anyio.ClosedResourceError

    client._session = _RetiredUnderneath()

    result = await client.call_tool("echo", {"text": "resent"})
    await _drained(client)

    assert result.content == "resent"
    assert spy.events == ["success"]
    assert (client.opens, client.closes) == (2, 2)


@pytest.mark.timeout(30)
async def test_retired_path_logged_once_per_client(caplog: pytest.LogCaptureFixture) -> None:
    client = await _retired_client()

    with caplog.at_level(logging.INFO, logger=mcp_mod.logger.name):
        await client.call_tool("echo", {"text": "a"})
        await client.call_tool("echo", {"text": "b"})
        await client.list_tools()

    retired = [r for r in caplog.records if "mcp.retired_client_call" in r.getMessage()]
    assert len(retired) == 1
    assert retired[0].levelno == logging.INFO
    assert "server=deep-ai-health-mcp" in retired[0].getMessage()


# ---------------------------------------------------------------------------
# close() — hard stop
# ---------------------------------------------------------------------------


async def _assert_hard_stop(client: _CountingClient) -> None:
    opens = client.opens
    with pytest.raises(RuntimeError, match="not started"):
        await client.call_tool("echo", {"text": "x"})
    with pytest.raises(RuntimeError, match="not started"):
        await client.list_tools()
    assert client.opens == opens  # nothing dialed


@pytest.mark.timeout(30)
async def test_close_is_a_hard_stop() -> None:
    """Review M2: invalidation (server removed / disabled / disconnected) and
    shutdown close the client — later calls must not reach the server."""
    client = _client()
    await client.start()
    await client.close()

    await _assert_hard_stop(client)


@pytest.mark.timeout(30)
async def test_retire_then_close_is_a_hard_stop() -> None:
    client = await _retired_client()
    await client.call_tool("echo", {"text": "served while retired"})

    await client.close()  # e.g. app shutdown

    await _assert_hard_stop(client)


@pytest.mark.timeout(30)
async def test_retire_after_close_stays_a_hard_stop() -> None:
    client = _client()
    await client.start()
    await client.close()

    await client.retire()

    await _assert_hard_stop(client)


@pytest.mark.timeout(30)
async def test_close_during_one_shot_handshake_stops_the_call(mcp_calls: list[str]) -> None:
    """Review-2 LOW-1: ``close()`` lands while a retired call is still opening
    its one-shot session. The call must not reach the server: the fresh
    session is torn down and the call fails "not started"."""
    client = await _retired_client()
    client.open_gate = asyncio.Event()
    task = asyncio.create_task(client.call_tool("echo", {"text": "revoked"}))
    while client.opens < 2:
        await asyncio.sleep(0.01)

    await client.close()  # invalidation / shutdown mid-handshake
    client.open_gate.set()

    with pytest.raises(RuntimeError, match="not started"):
        _ = await task
    assert _ECHO_CALLS == []
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


async def test_never_started_client_still_raises_not_started() -> None:
    client = _client()

    with pytest.raises(RuntimeError, match="not started"):
        await client.call_tool("echo", {"text": "x"})
    with pytest.raises(RuntimeError, match="not started"):
        await client.list_tools()
    assert client.opens == 0


# ---------------------------------------------------------------------------
# MCPServerPool.retire_all
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_pool_retire_all_retires_remote_and_closes_the_rest() -> None:
    remote = _client()
    await remote.start()
    recording = mcp_mod.RecordingMCPClient(tools=())
    pool = mcp_mod.MCPServerPool()
    await pool.add("remote", remote)
    await pool.add("recording", recording)

    await pool.retire_all()

    assert pool.names() == []
    assert recording.closed is True  # no retire(): falls back to close()
    result = await remote.call_tool("echo", {"text": "retired, not closed"})
    assert result.content == "retired, not closed"


# ---------------------------------------------------------------------------
# Review-2 LOW-3 — a live list_tools in flight at retire()
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_live_list_tools_is_bounded_by_timeout_s() -> None:
    """The SDK does not fail a request whose session was torn down underneath
    it (``retire()`` / ``close()``): the live ``list_tools`` must still end
    within ``timeout_s`` instead of hanging the agent build."""
    client = _client(timeout_s=0.5)
    await client.start()

    class _Stranded:
        async def list_tools(self) -> Any:
            await asyncio.Event().wait()  # the response never arrives

    client._session = _Stranded()

    started = time.monotonic()
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        await asyncio.wait_for(client.list_tools(), timeout=5)
    assert time.monotonic() - started < 3.0
    await client.close()  # releases the real session start() opened


@pytest.mark.timeout(30)
async def test_list_tools_stream_death_racing_retire_uses_a_one_shot_session() -> None:
    client = _client()
    await client.start()

    class _RetiredUnderneath:
        async def list_tools(self) -> Any:
            await client.retire()
            raise anyio.ClosedResourceError

    client._session = _RetiredUnderneath()

    tools = await client.list_tools()
    await _drained(client)

    assert sorted(t.name for t in tools) == ["echo", "gated", "slow"]
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


# ---------------------------------------------------------------------------
# B-248 — retire() drains the calls already running on the live session
# ---------------------------------------------------------------------------


async def _start_gated_call(client: _CountingClient, key: str) -> asyncio.Task[Any]:
    """A live ``call_tool`` the server has started and is still running."""
    task = asyncio.create_task(client.call_tool("gated", {"key": key}))
    while key not in _GATED_CALLS:
        await asyncio.sleep(0.01)
    return task


@pytest.mark.timeout(30)
async def test_retire_lets_an_in_flight_call_finish_on_its_session(mcp_calls: list[str]) -> None:
    """Pool TTL expiry while a call is running on the long-lived session. It
    used to tear the session down under the call: the response never arrived,
    the call failed after ``timeout_s`` although the server had run the tool."""
    client = _client(timeout_s=1.0)
    await client.start()
    task = await _start_gated_call(client, "a")

    await client.retire()

    assert client.closes == 0  # parked for the call, not torn down under it
    _GATES["a"].set()
    result = await asyncio.wait_for(task, timeout=5)
    await _drained(client)
    assert result.content == "opened a"
    assert mcp_calls == ["ok"]
    assert (client.opens, client.closes) == (1, 1)  # the last one out closed it
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_draining_session_closes_after_the_last_call_only() -> None:
    client = _client()
    await client.start()
    first = await _start_gated_call(client, "a")
    second = await _start_gated_call(client, "b")
    await client.retire()

    _GATES["a"].set()
    assert (await first).content == "opened a"
    assert client.closes == 0  # "b" still running on it

    # A call after retire() goes out on its own one-shot session.
    assert (await client.call_tool("echo", {"text": "new"})).content == "new"
    assert (client.opens, client.closes) == (2, 1)

    _GATES["b"].set()
    assert (await second).content == "opened b"
    await _drained(client)
    assert (client.opens, client.closes) == (2, 2)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_retire_drains_an_in_flight_list_tools() -> None:
    client = _client()
    await client.start()
    live = client._session
    entered, gate = asyncio.Event(), asyncio.Event()

    class _Gated:
        async def list_tools(self) -> Any:
            entered.set()
            await gate.wait()
            return await live.list_tools()

    client._session = _Gated()
    task = asyncio.create_task(client.list_tools())
    await entered.wait()

    await client.retire()

    assert client.closes == 0
    gate.set()
    tools = await asyncio.wait_for(task, timeout=5)
    await _drained(client)
    assert sorted(t.name for t in tools) == ["echo", "gated", "slow"]
    assert (client.opens, client.closes) == (1, 1)  # served by the parked session
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_close_hard_stops_a_draining_session() -> None:
    """Invalidation / shutdown while a retired session drains: it closes now,
    not when the in-flight call ends."""
    client = _client(timeout_s=0.5)
    await client.start()
    task = await _start_gated_call(client, "a")
    await client.retire()

    await client.close()

    assert client.closes == 1
    with pytest.raises(mcp_mod.MCPCallTimeoutError):
        _ = await asyncio.wait_for(task, timeout=5)
    assert client.closes == 1  # not closed a second time by the last caller
    await _assert_hard_stop(client)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_cancelled_in_flight_call_still_closes_the_draining_session() -> None:
    client = _client(timeout_s=10)
    await client.start()
    task = await _start_gated_call(client, "a")
    await client.retire()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    await _drained(client)

    assert (client.opens, client.closes) == (1, 1)
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_last_call_returns_before_the_drained_session_is_torn_down() -> None:
    """Review L3: the drained session's teardown (up to ``min(timeout_s, 5)``
    for a stuck DELETE) runs in the background, not in the call; ``close()``
    still waits for it, so nothing outlives the client."""
    client = _client(timeout_s=2.0)
    client.mode = "hang_teardown"
    await client.start()
    task = await _start_gated_call(client, "a")
    await client.retire()

    _GATES["a"].set()
    started = time.monotonic()
    result = await asyncio.wait_for(task, timeout=5)

    assert result.content == "opened a"
    assert time.monotonic() - started < 1.0  # not held by the 2 s teardown budget
    assert client.closes == 0
    await client.close()
    assert client.closes == 1
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
async def test_close_cancelled_behind_a_reconnect_is_still_a_hard_stop(
    mcp_calls: list[str],
) -> None:
    """Review-3 M1: a BUG-17 reconnect holds the restart lock for its whole
    handshake. A close() waiting behind it can be cancelled (the bulk teardown
    budget); the reconnect must not then install a session that serves a
    connector the platform switched off."""
    client = _client()
    await client.start()

    class _DeadOnSend:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            raise anyio.ClosedResourceError

    client._session = _DeadOnSend()
    client.open_gate = asyncio.Event()  # the reconnect's handshake hangs
    call = asyncio.create_task(client.call_tool("echo", {"text": "after off"}))
    while client.opens < 2:
        await asyncio.sleep(0.01)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await client.close()  # stuck behind the restart lock, then cancelled
    client.open_gate.set()

    with pytest.raises(RuntimeError, match="not started"):
        _ = await call
    assert _ECHO_CALLS == []
    opens = client.opens
    with pytest.raises(RuntimeError, match="not started"):
        await client.call_tool("echo", {"text": "later"})
    assert client.opens == opens
    assert client.opens == client.closes
    assert _live_owner_tasks() == []


@pytest.mark.timeout(30)
@pytest.mark.parametrize("retired", [False, True])
async def test_close_waiting_for_the_restart_lock_already_stops_calls(retired: bool) -> None:
    """While close() waits for the restart lock, calls already fail "not
    started" and nothing is dialed — live or retired client alike."""
    client = await _retired_client() if retired else _client()
    if not retired:
        await client.start()
    opens = client.opens

    async with client._restart_lock:  # e.g. a reconnect in progress
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="not started"):
            await client.call_tool("echo", {"text": "x"})
    await closing

    assert client.opens == opens
    assert _ECHO_CALLS == []
    assert _live_owner_tasks() == []
