"""Unit tests for the MCP tool adapter (Stream E.9)."""

from __future__ import annotations

import pytest

from orchestrator import Tool, ToolContext
from orchestrator.tools import (
    DEFAULT_MAX_SERVERS,
    DEFAULT_MCP_CHAR_CAP,
    MCPClient,
    MCPServerConfig,
    MCPServerPool,
    MCPServerPoolLimitError,
    MCPTool,
    MCPToolDef,
    RecordingMCPClient,
    ToolNotFoundError,
    ToolRegistry,
    register_mcp_tools,
)

_CTX = ToolContext()


def _read_file_def() -> MCPToolDef:
    return MCPToolDef(
        name="read_file",
        description="Read a file from the workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


def _list_dir_def() -> MCPToolDef:
    return MCPToolDef(
        name="list_directory",
        description="List a directory.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


# ---------------------------------------------------------------------------
# MCPServerConfig
# ---------------------------------------------------------------------------


def test_server_config_requires_non_empty_command() -> None:
    with pytest.raises(ValueError, match="empty command"):
        MCPServerConfig(name="fs", command=[])


def test_server_config_accepts_command_and_env() -> None:
    cfg = MCPServerConfig(
        name="fs",
        command=("npx", "mcp-server-filesystem", "/workspace"),
        env={"DEBUG": "1"},
    )
    assert cfg.command[0] == "npx"
    assert cfg.env["DEBUG"] == "1"


# --- Capability Uplift Sprint #5 — MCPServerConfig transport extension ----


def test_server_config_default_transport_is_stdio() -> None:
    """Backward compat: existing operator JSON files have no transport field."""
    cfg = MCPServerConfig(name="fs", command=("npx", "mcp-server-fs"))
    assert cfg.transport == "stdio"


def test_server_config_stdio_rejects_url() -> None:
    with pytest.raises(ValueError, match=r"stdio.*url"):
        MCPServerConfig(
            name="fs",
            transport="stdio",
            command=("npx", "x"),
            url="https://example.com/mcp",
        )


def test_server_config_sse_requires_url() -> None:
    with pytest.raises(ValueError, match=r"sse.*url"):
        MCPServerConfig(name="time", transport="sse")


def test_server_config_sse_rejects_command() -> None:
    with pytest.raises(ValueError, match=r"sse.*command"):
        MCPServerConfig(
            name="time",
            transport="sse",
            url="https://example.com/sse",
            command=("npx", "x"),
        )


def test_server_config_streamable_http_requires_url() -> None:
    with pytest.raises(ValueError, match=r"streamable_http.*url"):
        MCPServerConfig(name="github", transport="streamable_http")


def test_server_config_streamable_http_accepts_url_and_headers() -> None:
    cfg = MCPServerConfig(
        name="github",
        transport="streamable_http",
        url="https://api.githubcopilot.com/mcp/",
        headers={"X-GitHub-Api-Version": "2025-11-28"},
    )
    assert cfg.url == "https://api.githubcopilot.com/mcp/"
    assert cfg.headers["X-GitHub-Api-Version"] == "2025-11-28"


def test_server_config_bearer_requires_token_ref() -> None:
    with pytest.raises(ValueError, match=r"bearer.*token_ref"):
        MCPServerConfig(
            name="github",
            transport="streamable_http",
            url="https://api.example.com/mcp",
            auth_type="bearer",
            auth_config={},
        )


def test_server_config_bearer_accepts_token_ref() -> None:
    cfg = MCPServerConfig(
        name="github",
        transport="streamable_http",
        url="https://api.example.com/mcp",
        auth_type="bearer",
        auth_config={"token_ref": "secret://mcp/github/api-token"},
    )
    assert cfg.auth_type == "bearer"
    assert cfg.auth_config["token_ref"] == "secret://mcp/github/api-token"


def test_server_config_oauth2_requires_client_id_and_scope() -> None:
    with pytest.raises(ValueError, match=r"oauth2.*client_id"):
        MCPServerConfig(
            name="linear",
            transport="streamable_http",
            url="https://mcp.linear.app/",
            auth_type="oauth2",
            auth_config={"scope": "read"},
        )
    with pytest.raises(ValueError, match=r"oauth2.*scope"):
        MCPServerConfig(
            name="linear",
            transport="streamable_http",
            url="https://mcp.linear.app/",
            auth_type="oauth2",
            auth_config={"client_id": "abc"},
        )


def test_server_config_oauth2_accepts_with_client_id_and_scope() -> None:
    """Storing oauth2 config is allowed; the runtime fail-fast (U-12)
    happens later in the factory when build_mcp_pool tries to connect."""
    cfg = MCPServerConfig(
        name="linear",
        transport="streamable_http",
        url="https://mcp.linear.app/",
        auth_type="oauth2",
        auth_config={"client_id": "expert-work", "scope": "read"},
    )
    assert cfg.auth_type == "oauth2"


def test_server_config_repr_redacts_headers_and_auth_config() -> None:
    """Secret 隔离 (Mini-ADR U-11): bearer token / auth headers must not
    appear in dataclass repr so logs / tracebacks never leak credentials."""
    cfg = MCPServerConfig(
        name="github",
        transport="streamable_http",
        url="https://api.example.com/mcp",
        headers={"Authorization": "Bearer SECRET-TOKEN-DO-NOT-LEAK"},
        auth_type="bearer",
        auth_config={"token_ref": "secret://mcp/github/api-token"},
    )
    rendered = repr(cfg)
    assert "SECRET-TOKEN-DO-NOT-LEAK" not in rendered
    assert "secret://mcp/github/api-token" not in rendered
    # name + transport + url are operator metadata, fine to show
    assert "github" in rendered
    assert "streamable_http" in rendered


# ---------------------------------------------------------------------------
# RecordingMCPClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_client_list_tools_returns_configured() -> None:
    client = RecordingMCPClient(tools=(_read_file_def(), _list_dir_def()))
    tools = await client.list_tools()
    assert [t.name for t in tools] == ["read_file", "list_directory"]


@pytest.mark.asyncio
async def test_recording_client_call_tool_returns_scripted() -> None:
    client = RecordingMCPClient(
        tools=(_read_file_def(),),
        responses={"read_file": "file contents"},
    )
    result = await client.call_tool("read_file", {"path": "/etc/passwd"})
    assert result.content == "file contents"
    assert result.is_error is False
    assert client.calls == [("read_file", {"path": "/etc/passwd"})]


@pytest.mark.asyncio
async def test_recording_client_unknown_call_raises() -> None:
    client = RecordingMCPClient(responses={"a": "x"})
    with pytest.raises(ToolNotFoundError):
        await client.call_tool("b", {})


@pytest.mark.asyncio
async def test_recording_client_error_tools_marked() -> None:
    client = RecordingMCPClient(
        responses={"read_file": "not found"},
        error_tools=frozenset({"read_file"}),
    )
    result = await client.call_tool("read_file", {})
    assert result.is_error is True


@pytest.mark.asyncio
async def test_recording_client_close_marks_closed() -> None:
    client = RecordingMCPClient()
    await client.close()
    assert client.closed is True


def test_recording_client_satisfies_protocol() -> None:
    assert isinstance(RecordingMCPClient(), MCPClient)


# ---------------------------------------------------------------------------
# MCPTool wrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tool_spec_is_namespaced() -> None:
    tool = MCPTool(
        client=RecordingMCPClient(),
        tool_def=_read_file_def(),
        server_name="fs",
    )
    assert tool.spec.name == "mcp__fs__read_file"
    assert tool.spec.description == "Read a file from the workspace."
    assert tool.spec.parameters["required"] == ["path"]


def test_mcp_tool_name_is_wire_safe() -> None:
    """The namespaced name must satisfy the vendor function-name grammar
    ``^[a-zA-Z][a-zA-Z0-9_-]{,63}$`` — strict OpenAI-compatible vendors
    (moonshot/kimi) 400 on ``:`` / ``.`` (the old ``mcp:<server>.<tool>`` form),
    lenient ones (bigmodel/glm) tolerate them. ``mcp_tool_name`` must produce a
    name accepted everywhere, folding any out-of-grammar character to ``_``."""
    import re

    from orchestrator.tools.mcp import MCP_TOOL_NAME_PREFIX, mcp_tool_name, parse_mcp_tool_name

    grammar = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")

    # A real amap-style server (dash) + snake_case tool: no folding, just prefix.
    assert mcp_tool_name("amap-maps", "maps_geo") == "mcp__amap-maps__maps_geo"
    # Out-of-grammar characters in either segment fold to ``_`` (colon, dot,
    # space, slash, CJK) — the exact chars that 400'd on moonshot/kimi.
    folded = mcp_tool_name("weird:srv", "get.weather now/地图")
    assert ":" not in folded and "." not in folded and " " not in folded
    for name in (mcp_tool_name("amap-maps", "maps_geo"), folded, mcp_tool_name("s", "t")):
        assert grammar.match(name), name
        assert len(name) <= 64
    # A pathologically long tool name is capped at 64 without breaking the grammar.
    long_name = mcp_tool_name("srv", "x" * 200)
    assert len(long_name) == 64 and grammar.match(long_name)
    # ``parse_mcp_tool_name`` recovers the server (first ``__`` after the prefix);
    # non-MCP names return ``None``.
    assert parse_mcp_tool_name("mcp__amap-maps__maps_geo") == ("amap-maps", "maps_geo")
    assert parse_mcp_tool_name("read_file") is None
    assert MCP_TOOL_NAME_PREFIX == "mcp__"


@pytest.mark.asyncio
async def test_mcp_tool_call_routes_to_underlying_client() -> None:
    client = RecordingMCPClient(
        tools=(_read_file_def(),),
        responses={"read_file": "hello world"},
    )
    tool = MCPTool(client=client, tool_def=_read_file_def(), server_name="fs")
    result = await tool.call({"path": "/x"}, ctx=_CTX)

    assert result.content == "hello world"
    assert result.meta["server"] == "fs"
    assert result.meta["truncated"] is False
    assert result.meta["is_error"] is False
    # The underlying client got the raw tool name (no namespace).
    assert client.calls == [("read_file", {"path": "/x"})]


@pytest.mark.asyncio
async def test_mcp_tool_error_result_propagates_to_meta() -> None:
    client = RecordingMCPClient(
        tools=(_read_file_def(),),
        responses={"read_file": "permission denied"},
        error_tools=frozenset({"read_file"}),
    )
    tool = MCPTool(client=client, tool_def=_read_file_def(), server_name="fs")
    result = await tool.call({"path": "/etc/shadow"}, ctx=_CTX)
    assert result.meta["is_error"] is True
    # Content still surfaced so the LLM can react.
    assert "permission denied" in result.content


@pytest.mark.asyncio
async def test_mcp_tool_propagates_client_exceptions() -> None:
    """Mini-ADR E-12: tools let exceptions bubble so E.6 graph wraps."""
    client = RecordingMCPClient(
        responses={"read_file": ""},
        raise_on_call={"read_file": RuntimeError("subprocess died")},
    )
    tool = MCPTool(client=client, tool_def=_read_file_def(), server_name="fs")
    with pytest.raises(RuntimeError, match="subprocess died"):
        await tool.call({"path": "/x"}, ctx=_CTX)


@pytest.mark.asyncio
async def test_mcp_tool_satisfies_tool_protocol() -> None:
    tool = MCPTool(client=RecordingMCPClient(), tool_def=_read_file_def(), server_name="fs")
    assert isinstance(tool, Tool)


# ---------------------------------------------------------------------------
# Middle-trim truncation (Mini-ADR E-10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_content_passes_through_untrimmed() -> None:
    client = RecordingMCPClient(responses={"read_file": "small body"})
    tool = MCPTool(client=client, tool_def=_read_file_def(), server_name="fs")
    result = await tool.call({"path": "/x"}, ctx=_CTX)
    assert result.content == "small body"
    assert result.meta["truncated"] is False
    # Un-truncated output carries no overflow payload (Stream CM-5).
    assert result.full_content is None


@pytest.mark.asyncio
async def test_long_content_middle_trimmed_with_head_and_tail_visible() -> None:
    head_marker = "HEAD-START"
    tail_marker = "TAIL-END"
    middle = "x" * (DEFAULT_MCP_CHAR_CAP + 5_000)
    payload = head_marker + middle + tail_marker
    client = RecordingMCPClient(responses={"read_file": payload})
    tool = MCPTool(client=client, tool_def=_read_file_def(), server_name="fs")
    result = await tool.call({"path": "/x"}, ctx=_CTX)

    assert result.meta["truncated"] is True
    assert head_marker in result.content
    assert tail_marker in result.content
    assert "chars truncated" in result.content
    # Output is roughly the cap plus the truncation marker length.
    assert len(result.content) < DEFAULT_MCP_CHAR_CAP + 200
    # Stream CM-5: the complete payload rides along for externalization.
    assert result.full_content == payload


# ---------------------------------------------------------------------------
# register_mcp_tools helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_mcp_tools_registers_each_with_namespaced_name() -> None:
    client = RecordingMCPClient(
        tools=(_read_file_def(), _list_dir_def()),
        responses={"read_file": "r", "list_directory": "l"},
    )
    registry = ToolRegistry()
    registered = await register_mcp_tools(
        server_name="fs",
        client=client,
        registry=registry,
    )

    assert registered == ["mcp__fs__read_file", "mcp__fs__list_directory"]
    assert "mcp__fs__read_file" in registry
    assert "mcp__fs__list_directory" in registry
    # Dispatchable end-to-end through the registry.
    tool = registry.get_required("mcp__fs__read_file")
    result = await tool.call({"path": "/x"}, ctx=_CTX)
    assert result.content == "r"


# ---------------------------------------------------------------------------
# MCPServerPool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_add_and_get() -> None:
    pool = MCPServerPool()
    client = RecordingMCPClient()
    await pool.add("fs", client)
    assert pool.get("fs") is client
    assert pool.names() == ["fs"]


@pytest.mark.asyncio
async def test_pool_rejects_duplicate_name() -> None:
    pool = MCPServerPool()
    await pool.add("fs", RecordingMCPClient())
    with pytest.raises(ValueError, match="already registered"):
        await pool.add("fs", RecordingMCPClient())


@pytest.mark.asyncio
async def test_pool_enforces_max_servers_cap() -> None:
    pool = MCPServerPool(max_servers=2)
    await pool.add("a", RecordingMCPClient())
    await pool.add("b", RecordingMCPClient())
    with pytest.raises(MCPServerPoolLimitError, match="2 servers"):
        await pool.add("c", RecordingMCPClient())


@pytest.mark.asyncio
async def test_pool_default_max_matches_design() -> None:
    """STREAM-E-DESIGN § 6 caps the per-pool MCP server count at 5."""
    pool = MCPServerPool()
    assert pool.max_servers == DEFAULT_MAX_SERVERS


@pytest.mark.asyncio
async def test_pool_close_all_closes_every_client() -> None:
    pool = MCPServerPool()
    clients = [RecordingMCPClient() for _ in range(3)]
    for i, c in enumerate(clients):
        await pool.add(f"s{i}", c)
    await pool.close_all()
    assert all(c.closed for c in clients)
    assert pool.names() == []


@pytest.mark.asyncio
async def test_pool_close_all_collects_errors_into_exceptiongroup() -> None:
    class _BrokenClient:
        closed = False

        async def list_tools(self):
            return ()

        async def call_tool(self, name, args):
            return None  # never reached in this test

        async def close(self):
            raise RuntimeError("teardown fail")

    pool = MCPServerPool()
    await pool.add("good", RecordingMCPClient())
    await pool.add("bad", _BrokenClient())  # type: ignore[arg-type]
    with pytest.raises(ExceptionGroup) as excinfo:
        await pool.close_all()
    assert any(isinstance(e, RuntimeError) for e in excinfo.value.exceptions)
    # Even with the broken client, the pool empties.
    assert pool.names() == []


# ---------------------------------------------------------------------------
# MCP infra hardening — list_tools size caps (audit #5)
# ---------------------------------------------------------------------------


class _RawTool:
    """Minimal stand-in for an SDK tool object (``.name/.description/.inputSchema``)."""

    def __init__(self, name: str, description: str = "", input_schema: object = None) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema if input_schema is not None else {}


def test_materialize_tool_defs_caps_tool_count() -> None:
    from orchestrator.tools.mcp import (
        DEFAULT_MAX_TOOLS_PER_SERVER,
        _materialize_tool_defs,
    )

    raw = [_RawTool(f"t{i}") for i in range(DEFAULT_MAX_TOOLS_PER_SERVER + 25)]
    defs = _materialize_tool_defs(raw, server="evil")
    assert len(defs) == DEFAULT_MAX_TOOLS_PER_SERVER


def test_materialize_tool_defs_truncates_description() -> None:
    from orchestrator.tools.mcp import (
        DEFAULT_MAX_TOOL_DESC_CHARS,
        _materialize_tool_defs,
    )

    raw = [_RawTool("t", description="x" * (DEFAULT_MAX_TOOL_DESC_CHARS + 5000))]
    defs = _materialize_tool_defs(raw, server="evil")
    assert len(defs[0].description) == DEFAULT_MAX_TOOL_DESC_CHARS


def test_materialize_tool_defs_drops_oversized_schema() -> None:
    from orchestrator.tools.mcp import (
        DEFAULT_MAX_TOOL_SCHEMA_CHARS,
        _materialize_tool_defs,
    )

    huge = {"blob": "y" * (DEFAULT_MAX_TOOL_SCHEMA_CHARS + 1000)}
    raw = [_RawTool("t", input_schema=huge)]
    defs = _materialize_tool_defs(raw, server="evil")
    assert defs[0].input_schema == {}


# ---------------------------------------------------------------------------
# MCP infra hardening — remote circuit breaker wiring (audit #3)
# ---------------------------------------------------------------------------


class _RaisingSession:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name, args):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise ConnectionError("server down")


@pytest.mark.asyncio
async def test_remote_client_circuit_opens_and_short_circuits() -> None:
    from orchestrator.tools.mcp import (
        DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
        MCPServerUnhealthyError,
        SseMCPClient,
    )

    client = SseMCPClient(config=MCPServerConfig(name="down", transport="sse", url="https://x/y"))
    sess = _RaisingSession()
    client._session = sess  # type: ignore[attr-defined]

    # Each real failure propagates the transport error and trips the breaker.
    for _ in range(DEFAULT_CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(ConnectionError):
            await client.call_tool("t", {})
    assert sess.calls == DEFAULT_CIRCUIT_FAILURE_THRESHOLD
    assert client._breaker.state == "open"  # type: ignore[attr-defined]

    # Breaker open: the next call is short-circuited WITHOUT a round-trip.
    with pytest.raises(MCPServerUnhealthyError):
        await client.call_tool("t", {})
    assert sess.calls == DEFAULT_CIRCUIT_FAILURE_THRESHOLD  # unchanged


@pytest.mark.asyncio
async def test_remote_client_circuit_half_open_recovers() -> None:
    from orchestrator.tools.mcp import MCPCircuitBreaker, SseMCPClient

    clock = {"t": 0.0}
    client = SseMCPClient(config=MCPServerConfig(name="flap", transport="sse", url="https://x/y"))
    # Swap in a breaker with an injectable clock + tiny window so we can
    # drive the open -> half_open -> closed transition deterministically.
    client._breaker = MCPCircuitBreaker(  # type: ignore[attr-defined]
        server="flap", failure_threshold=1, window_s=10.0, now=lambda: clock["t"]
    )

    failing = _RaisingSession()
    client._session = failing  # type: ignore[attr-defined]
    with pytest.raises(ConnectionError):
        await client.call_tool("t", {})
    assert client._breaker.state == "open"  # type: ignore[attr-defined]

    # Advance past the window -> half_open allows one probe; a success closes it.
    clock["t"] = 20.0

    import types

    class _OkSession:
        async def call_tool(self, name, args):  # type: ignore[no-untyped-def]
            return types.SimpleNamespace(content=[], isError=False)

    client._session = _OkSession()  # type: ignore[attr-defined]
    result = await client.call_tool("t", {})
    assert result.is_error is False
    assert client._breaker.state == "closed"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# MCP infra hardening — stdio call_tool honors timeout_s (audit #7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_call_tool_times_out() -> None:
    import asyncio

    from orchestrator.tools.mcp import MCPCallTimeoutError, StdioMCPClient

    client = StdioMCPClient(
        config=MCPServerConfig(name="slow", transport="stdio", command=["echo"], timeout_s=0.01)
    )

    class _SlowSession:
        async def call_tool(self, name, args):  # type: ignore[no-untyped-def]
            await asyncio.sleep(1.0)

    client._session = _SlowSession()  # type: ignore[attr-defined]
    with pytest.raises(MCPCallTimeoutError):
        await client.call_tool("t", {})


# ---------------------------------------------------------------------------
# BUG-12 — bypass the SDK's output-schema validation (真栈事故 2026-08-24)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lenient_session_neuters_output_schema_validation() -> None:
    """BUG-12 (终审 F5 重构) — every transport constructs
    ``LenientClientSession``: the SDK's canonical send path is kept, and the
    single private validation hook ``_validate_tool_result`` is a no-op, so a
    server that declares ``outputSchema`` but returns only text content no
    longer kills the call. The class-construction assert also pins the hook's
    existence against an X-11 SDK bump."""
    from mcp import ClientSession
    from mcp import types as mcp_types

    from orchestrator.tools.mcp import _lenient_client_session_cls

    lenient_cls = _lenient_client_session_cls()
    assert issubclass(lenient_cls, ClientSession)

    # The SDK's own hook raises on this result; the lenient override must not.
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="plain text ok")],
        isError=False,
    )
    # Unbound-call the two hooks on a bare instance — the override reads no
    # session state, and the SDK original consults _tool_output_schemas which
    # we seed to force the "schema declared, no structured content" branch.
    bare = object.__new__(lenient_cls)
    bare._tool_output_schemas = {"t": {"type": "object"}}
    await lenient_cls._validate_tool_result(bare, "t", result)  # no raise

    with pytest.raises(RuntimeError, match="did not return structured content"):
        await ClientSession._validate_tool_result(bare, "t", result)
