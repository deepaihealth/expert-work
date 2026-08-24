"""MCP (Model Context Protocol) tool adapter — Stream E.9.

Wraps Anthropic's official ``mcp`` Python SDK so per-tenant MCP
servers configured via ``tenant_config.mcp_servers`` (E.8 migration
0011) get exposed to the orchestrator's :class:`ToolRegistry`. Per
Mini-ADR E-5, M0 ships **stdio transport only** — each MCP server is a
local subprocess. HTTP / SSE transports land in M1.

Components:

- :class:`MCPClient` Protocol — minimum surface the rest of the
  orchestrator depends on. ``RecordingMCPClient`` is the in-memory
  fake used by unit tests; ``StdioMCPClient`` is the production
  adapter around the SDK's ``stdio_client`` + ``ClientSession``
  context managers.

- :class:`MCPTool` — one **Expert Work** :class:`Tool` per MCP-exposed tool,
  namespaced as ``mcp:<server>.<tool>`` so the LLM sees them as
  first-class entries in the spec list.

- :class:`MCPServerPool` — lifecycle owner. Enforces an N=5 server
  cap per § 6 "MCP stdio 子进程泄漏" of
  [STREAM-E-DESIGN](../../../../../docs/streams/STREAM-E-DESIGN.md),
  and runs each server inside an ``AsyncExitStack`` so subprocess
  exits propagate cleanly on shutdown.

- :func:`register_mcp_tools` — helper that runs ``list_tools`` on a
  live :class:`MCPClient` and registers each as an :class:`MCPTool`.

Output truncation per Mini-ADR E-10 / § 1.1 E.9: each ``call_tool``
result is rendered to text (TextContent blocks concatenated) and, if
over 20 000 chars, **middle-trimmed** with the head 50 % + a
``[N chars truncated]`` placeholder + the tail 50 %. Middle trim
(not head or tail) lets the LLM see both the start of the response
(typically status / preamble) and the end (often a conclusion).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from expert_work.common.uplift_metrics import (
    record_mcp_call,
    record_mcp_circuit_state,
)
from orchestrator.tools.registry import (
    ToolContext,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)

logger = logging.getLogger(__name__)

#: Wire-safe namespaced-name grammar for MCP tools. The OpenAI / Anthropic
#: function-name rule is ``^[a-zA-Z][a-zA-Z0-9_-]{,63}$``: strict OpenAI-compatible
#: vendors (moonshot/kimi) return a 400 on ``:`` / ``.`` while lenient ones
#: (bigmodel/glm) tolerate them, so the old ``mcp:<server>.<tool>`` form worked on
#: some vendors and 400'd on others. ``mcp__<server>__<tool>`` — with every
#: out-of-grammar character folded to ``_`` and the whole capped at 64 chars — is
#: accepted everywhere. The ``mcp__`` prefix guarantees a letter start and still
#: marks a name as MCP-routed for the metric / audit helpers.
MCP_TOOL_NAME_PREFIX = "mcp__"
_MCP_NAME_SEP = "__"
_MCP_NAME_MAX_CHARS = 64
_MCP_UNSAFE_CHAR = re.compile(r"[^a-zA-Z0-9_-]")


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Compose the wire-safe Expert Work-side name for one MCP tool."""
    server = _MCP_UNSAFE_CHAR.sub("_", server_name)
    tool = _MCP_UNSAFE_CHAR.sub("_", tool_name)
    return f"{MCP_TOOL_NAME_PREFIX}{server}{_MCP_NAME_SEP}{tool}"[:_MCP_NAME_MAX_CHARS]


def parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Split a namespaced MCP tool name into ``(server, tool)``.

    ``None`` for non-MCP names. The FIRST ``__`` after the ``mcp__`` prefix
    separates server from tool — server names are catalog-curated and never
    contain ``__``, so the server segment is unambiguous even when the (folded)
    tool segment does. A name with no separator yields ``(server, "")``.
    """
    if not name.startswith(MCP_TOOL_NAME_PREFIX):
        return None
    server, _, tool = name[len(MCP_TOOL_NAME_PREFIX) :].partition(_MCP_NAME_SEP)
    return server, tool


DEFAULT_MCP_CHAR_CAP = 20_000
DEFAULT_MAX_SERVERS = 5
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_RETRY_MAX = 3
# Mini-ADR U-13: per-server circuit breaker thresholds.
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_WINDOW_S: float = 30 * 60  # 30 minutes
# Defensive caps on a server-advertised tool list. A remote MCP server is
# tenant-controlled and ``list_tools`` is materialised in-process (including in
# the control-plane probe), so an unbounded response is a memory-exhaustion
# vector (security audit #5). Cap the tool count and per-field size.
DEFAULT_MAX_TOOLS_PER_SERVER = 256
DEFAULT_MAX_TOOL_DESC_CHARS = 16_384
DEFAULT_MAX_TOOL_SCHEMA_CHARS = 65_536
_TRUNCATION_PREFIX = "...["
_TRUNCATION_SUFFIX = " chars truncated]..."

MCPTransport = Literal["stdio", "sse", "streamable_http"]
MCPAuthType = Literal["none", "bearer", "oauth2"]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MCPCallTimeoutError(TimeoutError):
    """Raised when a single ``call_tool`` exceeds the configured timeout."""


class MCPServerUnhealthyError(RuntimeError):
    """Raised when the circuit breaker is open and rejects a call."""


class MCPOAuthNotImplementedError(NotImplementedError):
    """Raised at boot when a server is configured with ``auth_type=oauth2``.

    Mini-ADR U-12: the schema accepts oauth2 configs but the flow itself
    (authorization code / refresh / per-tenant token store) ships in the
    follow-up Mini-ADR L.L8-MCP sprint. Fail-fast at boot beats silently
    skipping a server the operator believed was online.
    """


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPServerConfig:
    """Per-server launch config from the platform JSON file
    (``mcp_servers_config_file``, Mini-ADR E-17 — operator-controlled).

    ``transport`` selects between local subprocess (``stdio``, default
    for backward compat with the original Stream E.9 shape) and the
    remote MCP transports added in Capability Uplift Sprint #5
    (``sse`` / ``streamable_http``). Per-tenant ``mcp_servers`` may
    still only **enable / filter** entries from this central pool — the
    URL / headers / auth fields are operator-controlled to keep tenants
    from injecting exfiltration targets (sanity check echoed in
    :mod:`services.control_plane.runtime` loader).

    ``headers`` and ``auth_config`` are :class:`field` ``repr=False``
    so a stray ``logger.exception(cfg)`` cannot leak the bearer token
    or token reference (Mini-ADR U-11).
    """

    name: str
    transport: MCPTransport = "stdio"
    # stdio fields
    command: Sequence[str] | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    # sse / streamable_http fields
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    # auth
    auth_type: MCPAuthType = "none"
    auth_config: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # failure handling (Mini-ADR U-13)
    timeout_s: float = DEFAULT_TIMEOUT_S
    retry_max: int = DEFAULT_RETRY_MAX
    # SSE read timeout — long-lived SSE streams idle between events; this caps
    # the per-read wait independently of ``timeout_s`` (the connect/call cap).
    # ``None`` keeps the SDK default (300s). Only meaningful for the sse transport.
    sse_read_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if self.transport == "stdio":
            if not self.command:
                # Preserve the historical message — Stream E.9 callers
                # match on "empty command" via test fixtures.
                msg = f"mcp server {self.name!r} has empty command"
                raise ValueError(msg)
            if self.url is not None:
                msg = (
                    f"mcp server {self.name!r}: stdio transport must not "
                    "set url (URL is for sse/streamable_http only)"
                )
                raise ValueError(msg)
        else:
            # sse / streamable_http
            if not self.url:
                msg = f"mcp server {self.name!r}: {self.transport} transport requires url"
                raise ValueError(msg)
            if self.command is not None:
                msg = (
                    f"mcp server {self.name!r}: {self.transport} transport "
                    "must not set command (command is for stdio only)"
                )
                raise ValueError(msg)
        if self.auth_type == "bearer" and "token_ref" not in self.auth_config:
            msg = (
                f"mcp server {self.name!r}: bearer auth requires "
                'auth_config["token_ref"] pointing at a secret:// URI'
            )
            raise ValueError(msg)
        if self.auth_type == "oauth2":
            if "client_id" not in self.auth_config:
                msg = (
                    f"mcp server {self.name!r}: oauth2 auth requires "
                    'auth_config["client_id"] (see Mini-ADR U-12)'
                )
                raise ValueError(msg)
            if "scope" not in self.auth_config:
                msg = (
                    f"mcp server {self.name!r}: oauth2 auth requires "
                    'auth_config["scope"] (see Mini-ADR U-12)'
                )
                raise ValueError(msg)


@dataclass(frozen=True)
class MCPToolDef:
    """A single tool advertised by an MCP server's ``list_tools``."""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class MCPCallResult:
    """Outcome of an MCP ``call_tool`` round-trip.

    ``content`` is the textified payload — concatenated TextContent
    blocks from the SDK's ``CallToolResult``. Non-text content
    (images, embedded resources) is dropped in M0 and noted by the
    adapter; multimodal lands with M2 / M3.
    """

    content: str
    is_error: bool = False


def _materialize_tool_defs(raw_tools: Sequence[Any], *, server: str) -> tuple[MCPToolDef, ...]:
    """Map SDK tool objects to :class:`MCPToolDef` with defensive caps.

    A remote MCP server is tenant-controlled and its ``list_tools`` response is
    materialised in-process (notably in the control-plane probe), so an
    unbounded reply is a memory-exhaustion vector. Cap the tool count and the
    per-field size; oversized schemas are dropped rather than carried as
    megabytes of attacker-controlled JSON (security audit #5).
    """
    defs: list[MCPToolDef] = []
    for t in raw_tools:
        if len(defs) >= DEFAULT_MAX_TOOLS_PER_SERVER:
            logger.warning(
                "mcp.tool_list_truncated server=%s cap=%d",
                server,
                DEFAULT_MAX_TOOLS_PER_SERVER,
            )
            break
        description = str(getattr(t, "description", "") or "")
        if len(description) > DEFAULT_MAX_TOOL_DESC_CHARS:
            description = description[:DEFAULT_MAX_TOOL_DESC_CHARS]
        schema = dict(getattr(t, "inputSchema", {}) or {})
        try:
            oversized = len(json.dumps(schema)) > DEFAULT_MAX_TOOL_SCHEMA_CHARS
        except (TypeError, ValueError):
            oversized = True
        if oversized:
            logger.warning(
                "mcp.tool_schema_dropped server=%s tool=%s",
                server,
                getattr(t, "name", "?"),
            )
            schema = {}
        defs.append(MCPToolDef(name=str(t.name), description=description, input_schema=schema))
    return tuple(defs)


# ---------------------------------------------------------------------------
# Client protocol + impls
# ---------------------------------------------------------------------------


@runtime_checkable
class MCPClient(Protocol):
    """Minimum surface :class:`MCPTool` + :class:`MCPServerPool` need."""

    async def list_tools(self) -> Sequence[MCPToolDef]:
        """Return the tools the server is advertising."""

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any],
    ) -> MCPCallResult:
        """Dispatch a tool call. Implementations may raise on
        transport errors — :class:`MCPTool.call` lets them propagate
        so the ReAct ``tools`` node wraps as :class:`ToolMessage(error)`."""

    async def close(self) -> None:
        """Tear the connection down (subprocess kill in the stdio case)."""


@dataclass
class RecordingMCPClient:
    """In-memory :class:`MCPClient` used by tests + dev fixtures.

    Hand it a list of :class:`MCPToolDef` for ``list_tools`` plus a
    mapping of ``{tool_name → response_string}`` for ``call_tool``;
    omitted names raise :class:`ToolNotFoundError`.
    """

    tools: Sequence[MCPToolDef] = field(default_factory=tuple)
    responses: Mapping[str, str] = field(default_factory=dict)
    raise_on_call: Mapping[str, Exception] = field(default_factory=dict)
    error_tools: frozenset[str] = field(default_factory=frozenset)
    closed: bool = False
    calls: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    async def list_tools(self) -> Sequence[MCPToolDef]:
        return tuple(self.tools)

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any],
    ) -> MCPCallResult:
        self.calls.append((name, dict(args)))
        if name in self.raise_on_call:
            raise self.raise_on_call[name]
        if name not in self.responses:
            msg = f"recording client has no scripted response for {name!r}"
            raise ToolNotFoundError(msg)
        return MCPCallResult(
            content=self.responses[name],
            is_error=name in self.error_tools,
        )

    async def close(self) -> None:
        self.closed = True


def _lenient_client_session_cls() -> Any:
    """The :class:`mcp.ClientSession` subclass every transport constructs —
    identical wire behavior, with the output-schema validation neutered.

    BUG-12 (真栈事故 2026-08-24): the deep-ai-health MCP server declares an
    ``outputSchema`` on its tools but only returns text ``content`` (no
    ``structuredContent``). mcp SDK 1.x's ``ClientSession.call_tool`` runs
    ``_validate_tool_result`` on every non-error result and raises
    ``RuntimeError: Tool xxx has an output schema but did not return
    structured content`` — killing every tool call against that server.

    Overriding the single private validation hook (rather than hand-copying
    the send half of ``call_tool``, 终审 F5) keeps the SDK's canonical send
    path AND makes the bypass the default for every present and future call
    site — nothing can reintroduce the incident by calling
    ``session.call_tool`` directly.

    - **Why skipping validation is lossless**: the platform only consumes
      ``result.content`` text blocks (:func:`_render_content_blocks`);
      ``structuredContent`` has no consumer anywhere, so the check has only
      blast radius and zero benefit here.
    - **X-11 (mcp 2.x migration)**: the ``assert`` below fails loudly at
      construction if the SDK renames/removes the private hook — re-verify
      the override point before bumping.
    """
    # Lazy import — module top must stay SDK-free (existing convention:
    # orchestrator imports fine in contexts without the mcp SDK).
    from mcp import ClientSession
    from mcp import types as mcp_types

    if not hasattr(ClientSession, "_validate_tool_result"):
        msg = (
            "mcp SDK no longer has ClientSession._validate_tool_result — the "
            "BUG-12 lenient bypass must be re-verified against this SDK "
            "version (X-11)."
        )
        raise RuntimeError(msg)

    class LenientClientSession(ClientSession):
        async def _validate_tool_result(self, name: str, result: mcp_types.CallToolResult) -> None:
            """BUG-12 — deliberate no-op: never raise on missing/invalid
            structuredContent (see factory docstring)."""

    return LenientClientSession


@dataclass
class StdioMCPClient:
    """Production :class:`MCPClient` — wraps the mcp SDK stdio transport.

    Holds a single subprocess + ``ClientSession`` alive between
    ``start`` and ``close``. The session is managed via
    :class:`contextlib.AsyncExitStack` so subprocess teardown happens
    even if the orchestrator crashes mid-call.

    Not used directly by unit tests — they inject
    :class:`RecordingMCPClient` for determinism. The pool wires this
    one in production with real MCP server commands.
    """

    config: MCPServerConfig
    _stack: contextlib.AsyncExitStack | None = field(default=None, init=False, repr=False)
    _session: Any = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        """Launch the subprocess and complete the MCP handshake."""
        # Imports kept local so the orchestrator can be imported in
        # contexts where the mcp SDK isn't available (e.g. middleware-
        # only unit tests with no MCP wiring).
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        session_cls = _lenient_client_session_cls()

        if self._stack is not None:
            msg = f"StdioMCPClient {self.config.name!r} already started"
            raise RuntimeError(msg)

        command = self.config.command
        if command is None:  # pragma: no cover — guarded by post_init
            msg = (
                f"StdioMCPClient {self.config.name!r}: stdio transport "
                "requires command (impossible to reach: post_init enforces)"
            )
            raise RuntimeError(msg)
        params = StdioServerParameters(
            command=command[0],
            args=list(command[1:]),
            env=dict(self.config.env) or None,
        )
        stack = contextlib.AsyncExitStack()
        await stack.__aenter__()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(session_cls(read, write))
            await session.initialize()
        except BaseException:
            await stack.__aexit__(None, None, None)
            raise

        self._stack = stack
        self._session = session

    async def list_tools(self) -> Sequence[MCPToolDef]:
        if self._session is None:
            msg = f"StdioMCPClient {self.config.name!r} not started"
            raise RuntimeError(msg)
        result = await self._session.list_tools()
        return _materialize_tool_defs(result.tools, server=self.config.name)

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any],
    ) -> MCPCallResult:
        if self._session is None:
            msg = f"StdioMCPClient {self.config.name!r} not started"
            raise RuntimeError(msg)
        # Honor timeout_s like the remote transports do — a wedged stdio
        # subprocess must not hang the agent run indefinitely (audit #7).
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, dict(args)),
                timeout=self.config.timeout_s,
            )
        except TimeoutError as exc:
            msg = (
                f"mcp call timed out after {self.config.timeout_s}s "
                f"on {self.config.name!r}:{name!r}"
            )
            raise MCPCallTimeoutError(msg) from exc
        return MCPCallResult(
            content=_render_content_blocks(result.content),
            is_error=bool(getattr(result, "isError", False)),
        )

    async def close(self) -> None:
        if self._stack is None:
            return
        await self._stack.__aexit__(None, None, None)
        self._stack = None
        self._session = None


@dataclass
class _RemoteMCPClientBase:
    """Shared scaffolding for :class:`SseMCPClient` /
    :class:`StreamableHttpMCPClient`.

    Both transports converge on the same :class:`mcp.ClientSession` pattern
    after handshake — the per-transport difference is just which SDK
    helper opens the underlying streams. Subclasses implement
    :meth:`_open_streams` (an async-context-managed (read, write[, ...])
    tuple) and inherit the rest from here.
    """

    config: MCPServerConfig
    resolved_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    _stack: contextlib.AsyncExitStack | None = field(default=None, init=False, repr=False)
    _session: Any = field(default=None, init=False, repr=False)
    #: Mini-ADR U-13 — per-server breaker so a down remote server short-circuits
    #: instead of eating the full ``timeout_s`` on every call (audit #3).
    _breaker: MCPCircuitBreaker = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._breaker = MCPCircuitBreaker(server=self.config.name)

    async def _open_streams(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        msg = "_open_streams must be implemented by transport subclass"
        raise NotImplementedError(msg)

    async def start(self) -> None:
        # Imports kept local so orchestrator can be imported in contexts
        # that don't have the mcp SDK on the path (e.g. middleware-only
        # unit tests with no MCP wiring).
        session_cls = _lenient_client_session_cls()

        if self._stack is not None:
            msg = f"{type(self).__name__} {self.config.name!r} already started"
            raise RuntimeError(msg)
        stack = contextlib.AsyncExitStack()
        await stack.__aenter__()
        try:
            read, write = await self._open_streams(stack)
            session = await stack.enter_async_context(session_cls(read, write))
            await session.initialize()
        except BaseException:
            await stack.__aexit__(None, None, None)
            raise
        self._stack = stack
        self._session = session

    async def list_tools(self) -> Sequence[MCPToolDef]:
        if self._session is None:
            msg = f"{type(self).__name__} {self.config.name!r} not started"
            raise RuntimeError(msg)
        result = await self._session.list_tools()
        return _materialize_tool_defs(result.tools, server=self.config.name)

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any],
    ) -> MCPCallResult:
        if self._session is None:
            msg = f"{type(self).__name__} {self.config.name!r} not started"
            raise RuntimeError(msg)
        transport = self.config.transport
        server = self.config.name
        # Circuit breaker (audit #3): a server that has tripped open is
        # short-circuited without a round-trip, so a persistently-down server
        # stops draining ``timeout_s`` on every call.
        if not self._breaker.allow_call():
            record_mcp_call(transport=transport, server=server, result="circuit_open")
            msg = f"mcp server {server!r} circuit open — skipping call to {name!r}"
            raise MCPServerUnhealthyError(msg)
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, dict(args)),
                timeout=self.config.timeout_s,
            )
        except TimeoutError as exc:
            self._breaker.record_failure()
            record_mcp_call(transport=transport, server=server, result="timeout")
            msg = f"mcp call timed out after {self.config.timeout_s}s on {server!r}:{name!r}"
            raise MCPCallTimeoutError(msg) from exc
        except Exception:
            self._breaker.record_failure()
            record_mcp_call(transport=transport, server=server, result="transport_err")
            raise
        self._breaker.record_success()
        record_mcp_call(transport=transport, server=server, result="ok")
        return MCPCallResult(
            content=_render_content_blocks(result.content),
            is_error=bool(getattr(result, "isError", False)),
        )

    async def close(self) -> None:
        if self._stack is None:
            return
        await self._stack.__aexit__(None, None, None)
        self._stack = None
        self._session = None


@dataclass
class SseMCPClient(_RemoteMCPClientBase):
    """:class:`MCPClient` over the SSE transport (legacy MCP HTTP form).

    Wraps ``mcp.client.sse.sse_client``. ``resolved_headers`` must
    already have the bearer token / api-key injected by the runtime
    factory (the dataclass itself never sees the secret value — see
    Mini-ADR U-11).
    """

    async def _open_streams(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        from mcp.client.sse import sse_client

        if not self.config.url:  # pragma: no cover — guarded by post_init
            msg = "sse transport requires url"
            raise RuntimeError(msg)
        sse_kwargs: dict[str, Any] = {
            "url": self.config.url,
            "headers": dict(self.resolved_headers) or None,
            "timeout": self.config.timeout_s,
        }
        if self.config.sse_read_timeout_s is not None:
            sse_kwargs["sse_read_timeout"] = self.config.sse_read_timeout_s
        read, write = await stack.enter_async_context(sse_client(**sse_kwargs))
        return read, write


@dataclass
class StreamableHttpMCPClient(_RemoteMCPClientBase):
    """:class:`MCPClient` over the modern StreamableHTTP transport.

    Wraps ``mcp.client.streamable_http.streamable_http_client`` (the
    canonical name as of mcp SDK ≥ 1.x; the older ``streamablehttp_client``
    spelling is deprecated). Headers / timeout are configured on a
    dedicated :class:`httpx.AsyncClient` so the SDK helper sees them
    through its single ``http_client`` parameter. The SDK yields
    ``(read, write, get_session_id)``; we drop the session callback
    because the orchestrator doesn't (yet) surface it — M1 may use it
    for resumption.
    """

    async def _open_streams(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        if not self.config.url:  # pragma: no cover — guarded by post_init
            msg = "streamable_http transport requires url"
            raise RuntimeError(msg)
        http_client = await stack.enter_async_context(
            httpx.AsyncClient(
                headers=dict(self.resolved_headers) or None,
                timeout=self.config.timeout_s,
            )
        )
        read, write, _get_session_id = await stack.enter_async_context(
            streamable_http_client(
                url=self.config.url,
                http_client=http_client,
            )
        )
        return read, write


def _render_content_blocks(blocks: Sequence[Any]) -> str:
    """Concatenate :class:`mcp.types.TextContent` blocks. Non-text
    content (images, embedded resources) is dropped in M0 — log it
    so M1/M2 multimodal work has a paper trail."""
    parts: list[str] = []
    skipped = 0
    for block in blocks:
        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if block_type == "text" and isinstance(text, str):
            parts.append(text)
        else:
            skipped += 1
    if skipped:
        logger.info("mcp.dropped_non_text_blocks count=%d", skipped)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Expert Work Tool wrapper
# ---------------------------------------------------------------------------


@dataclass
class MCPTool:
    """Expert Work :class:`Tool` wrapping one MCP-exposed tool.

    The Expert Work-side name is namespaced ``mcp__<server>__<tool>`` (see
    :func:`mcp_tool_name`) so the LLM (and the audit log) can tell which MCP
    server a call routed to, while staying inside the vendor function-name
    grammar. ``call`` ignores ``ctx`` in M0 — per-tenant MCP server
    selection happens at registration time (the orchestrator binds a
    pool per tenant); the tool itself only sees one server.
    """

    client: MCPClient
    tool_def: MCPToolDef
    server_name: str
    content_char_cap: int = DEFAULT_MCP_CHAR_CAP
    spec: ToolSpec = field(init=False)

    def __post_init__(self) -> None:
        # ``spec`` is derived from the other fields; we materialise it
        # here (rather than via @property) so the Tool Protocol's
        # ``spec: ToolSpec`` attribute contract is satisfied.
        object.__setattr__(
            self,
            "spec",
            ToolSpec(
                name=mcp_tool_name(self.server_name, self.tool_def.name),
                description=self.tool_def.description,
                parameters=self.tool_def.input_schema,
            ),
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        result = await self.client.call_tool(self.tool_def.name, args)
        content, truncated = _middle_trim(result.content, self.content_char_cap)
        return ToolResult(
            content=content,
            meta={
                "server": self.server_name,
                "truncated": truncated,
                "is_error": result.is_error,
            },
            # Stream CM-5: middle-trimmed MCP output is otherwise
            # unrecoverable — carry the full text for externalization.
            full_content=result.content if truncated else None,
        )


def _middle_trim(text: str, cap: int) -> tuple[str, bool]:
    """Middle-truncate to ``cap`` chars, keeping head + tail 50%.

    Returns ``(text, was_truncated)``. Letting the LLM see both ends
    of an over-cap response is the deer-flow ``_truncate_bash_output``
    pattern; with file contents in particular the head shows the
    metadata / shebang and the tail shows whatever the model was
    looking for (results, errors, EOF marker).
    """
    if len(text) <= cap:
        return text, False
    half = cap // 2
    dropped = len(text) - cap
    head = text[:half]
    tail = text[-half:]
    return (
        f"{head}\n{_TRUNCATION_PREFIX}{dropped}{_TRUNCATION_SUFFIX}\n{tail}",
        True,
    )


# ---------------------------------------------------------------------------
# Pool / lifecycle
# ---------------------------------------------------------------------------


class MCPServerPoolLimitError(RuntimeError):
    """Raised when adding a server would exceed ``max_servers``."""


@dataclass
class MCPServerPool:
    """Owns the live :class:`MCPClient` instances per tenant.

    M0 keeps things simple: one process-global pool. M1 promotes this
    to a per-tenant dict keyed by ``tenant_id`` (necessary once
    Sub-Agent + tenant isolation deepen). Enforces ``max_servers=5``
    per [STREAM-E-DESIGN § 6 risk table](../../../../../docs/streams/STREAM-E-DESIGN.md):
    each subprocess takes file descriptors + a Python interpreter
    (for npm-based servers), so the cap stops a runaway manifest from
    starving the host.
    """

    max_servers: int = DEFAULT_MAX_SERVERS
    _clients: dict[str, MCPClient] = field(default_factory=dict, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def add(self, name: str, client: MCPClient) -> None:
        async with self._lock:
            if name in self._clients:
                msg = f"mcp server {name!r} already registered"
                raise ValueError(msg)
            if len(self._clients) >= self.max_servers:
                msg = f"mcp pool already has {self.max_servers} servers; cannot add {name!r}"
                raise MCPServerPoolLimitError(msg)
            self._clients[name] = client

    def get(self, name: str) -> MCPClient | None:
        return self._clients.get(name)

    def names(self) -> list[str]:
        return list(self._clients.keys())

    async def close_all(self) -> None:
        async with self._lock:
            errors: list[Exception] = []
            for name, client in list(self._clients.items()):
                try:
                    await client.close()
                except Exception as exc:
                    logger.warning("mcp.close_failed server=%s err=%s", name, exc)
                    errors.append(exc)
            self._clients.clear()
            if errors:
                raise ExceptionGroup("mcp.close_errors", errors)


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


async def register_mcp_tools(
    *,
    server_name: str,
    client: MCPClient,
    registry: ToolRegistry,
    content_char_cap: int = DEFAULT_MCP_CHAR_CAP,
    allow_tools: Collection[str] | None = None,
    deferred: bool = False,
) -> list[str]:
    """List tools from ``client`` and register each as :class:`MCPTool`.

    ``allow_tools`` optionally filters by the server-advertised (bare)
    tool name — ``None`` registers every tool, a set registers only the
    listed ones (the manifest's ``MCPToolSpec.allow_tools``).

    ``deferred`` (Stream TE-6b) registers each MCP tool deferred — absent
    from the per-turn LLM bind until the model retrieves it via
    ``find_tools``. MCP servers are the main Context-Bloat source (one
    server can advertise dozens of verbose tool schemas), so the assembler
    passes ``deferred=True`` (deer-flow's always-defer-MCP policy).

    Returns the namespaced names registered, useful for audit
    attribution at orchestrator startup.
    """
    tools = await client.list_tools()
    registered: list[str] = []
    for tool_def in tools:
        if allow_tools is not None and tool_def.name not in allow_tools:
            continue
        expert_work_tool = MCPTool(
            client=client,
            tool_def=tool_def,
            server_name=server_name,
            content_char_cap=content_char_cap,
        )
        registry.register(expert_work_tool, deferred=deferred, source=f"mcp:{server_name}")
        registered.append(expert_work_tool.spec.name)
    logger.info("mcp.registered server=%s tools=%s", server_name, registered)
    return registered


# ---------------------------------------------------------------------------
# Circuit breaker (Mini-ADR U-13)
# ---------------------------------------------------------------------------


CircuitState = Literal["closed", "half_open", "open"]


@dataclass
class MCPCircuitBreaker:
    """Per-server failure isolator for the remote MCP transports.

    A remote MCP server going down (URL 5xx, connection refused, mid-
    stream SSE close) used to surface as a 30s timeout on every agent
    call, dragging the whole orchestrator's tool latency. The breaker
    flips to ``open`` after :attr:`failure_threshold` consecutive
    failures and short-circuits subsequent calls for
    :attr:`window_s` seconds, then probes a single half-open call to
    re-validate. A success closes; a failure re-opens the window.

    Thresholds mirror Envoy / Istio defaults (5 failures, 30-minute
    window) so operators familiar with mesh circuit breakers see
    expected behavior; tunables are constructor arguments so per-server
    overrides land cleanly when M1 surfaces per-tenant config.

    Time is injected via :attr:`now` so tests can advance the clock
    without real :func:`time.sleep`.
    """

    server: str
    failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD
    window_s: float = DEFAULT_CIRCUIT_WINDOW_S
    now: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _state: CircuitState = field(default="closed", init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow_call(self) -> bool:
        """Return ``True`` if the caller may attempt a real round-trip.

        Probes the half-open transition when the window has elapsed —
        side-effecting on purpose so the next ``record_success`` /
        ``record_failure`` resolves the probe outcome.
        """
        if self._state == "closed":
            return True
        if self._state == "half_open":
            return True
        # open: check window
        if self._opened_at is None:  # pragma: no cover — defensive
            return False
        if self.now() - self._opened_at >= self.window_s:
            self._state = "half_open"
            record_mcp_circuit_state(server=self.server, state="half_open")
            return True
        return False

    def record_success(self) -> None:
        """A real call returned cleanly — clear the breaker."""
        was_half_open = self._state == "half_open"
        self._failures = 0
        self._state = "closed"
        self._opened_at = None
        if was_half_open:
            record_mcp_circuit_state(server=self.server, state="closed")

    def record_failure(self) -> None:
        """A real call failed (timeout / transport error / 5xx).

        In ``closed`` state: increments and trips at the threshold.
        In ``half_open`` state: a single failure re-opens the window
        immediately (no need to wait for another threshold burst since
        we're already in failure mode).
        """
        if self._state == "half_open":
            self._open()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = self.now()
        record_mcp_circuit_state(server=self.server, state="open")
