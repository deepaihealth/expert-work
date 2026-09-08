"""Unit tests for the remote MCP connect-probe (Stream V-C)."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from control_plane.mcp_probe import McpProbeError, probe_remote_mcp
from orchestrator.tools.mcp import MCPToolDef


class _FakeClient:
    def __init__(
        self,
        *,
        tools=None,
        raise_on_start=None,
        raise_on_list=None,
        raise_on_close=None,
    ):
        self._tools = tools or []
        self._raise_on_start = raise_on_start
        self._raise_on_list = raise_on_list
        self._raise_on_close = raise_on_close
        self.closed = False

    async def start(self) -> None:
        if self._raise_on_start is not None:
            raise self._raise_on_start

    async def list_tools(self):
        if self._raise_on_list is not None:
            raise self._raise_on_list
        return tuple(self._tools)

    async def close(self) -> None:
        self.closed = True
        if self._raise_on_close is not None:
            raise self._raise_on_close


@pytest.mark.asyncio
async def test_probe_returns_tools_on_success() -> None:
    captured: dict[str, object] = {}

    def factory(config, headers):
        captured["transport"] = config.transport
        captured["headers"] = headers
        return _FakeClient(tools=[MCPToolDef(name="create_issue", description="", input_schema={})])

    tools = await probe_remote_mcp(
        name="github",
        transport="streamable_http",
        url="https://mcp.example.com/mcp",
        bearer_token="ghp_secret",
        timeout_s=10.0,
        client_factory=factory,
    )
    assert [t.name for t in tools] == ["create_issue"]
    assert captured["headers"]["Authorization"] == "Bearer ghp_secret"


@pytest.mark.asyncio
async def test_probe_no_auth_sends_no_authorization_header() -> None:
    captured: dict[str, object] = {}

    def factory(config, headers):
        captured["headers"] = headers
        return _FakeClient(tools=[])

    await probe_remote_mcp(
        name="open",
        transport="sse",
        url="https://mcp.example.com/sse",
        bearer_token=None,
        timeout_s=10.0,
        client_factory=factory,
    )
    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_probe_rejects_ssrf_url() -> None:
    with pytest.raises(McpProbeError) as ei:
        await probe_remote_mcp(
            name="evil",
            transport="streamable_http",
            url="http://169.254.169.254/latest",
            bearer_token=None,
            timeout_s=10.0,
            client_factory=lambda c, h: _FakeClient(),
        )
    assert ei.value.code == "MCP_SERVER_INVALID_URL"


@pytest.mark.asyncio
async def test_probe_wraps_connect_failure() -> None:
    def factory(config, headers):
        return _FakeClient(raise_on_start=RuntimeError("connection refused"))

    with pytest.raises(McpProbeError) as ei:
        await probe_remote_mcp(
            name="down",
            transport="streamable_http",
            url="https://down.example.com/mcp",
            bearer_token=None,
            timeout_s=10.0,
            client_factory=factory,
        )
    assert ei.value.code == "MCP_SERVER_PROBE_FAILED"


@pytest.mark.asyncio
async def test_probe_always_closes_client() -> None:
    client = _FakeClient(raise_on_list=RuntimeError("boom"))

    def factory(config, headers):
        return client

    with pytest.raises(McpProbeError):
        await probe_remote_mcp(
            name="x",
            transport="sse",
            url="https://x.example.com/sse",
            bearer_token=None,
            timeout_s=10.0,
            client_factory=factory,
        )
    assert client.closed is True


@pytest.mark.asyncio
async def test_probe_always_closes_client_on_start_failure() -> None:
    client = _FakeClient(raise_on_start=RuntimeError("refused"))
    with pytest.raises(McpProbeError):
        await probe_remote_mcp(
            name="x",
            transport="sse",
            url="https://x.example.com/sse",
            bearer_token=None,
            timeout_s=10.0,
            client_factory=lambda c, h: client,
        )
    assert client.closed is True


@pytest.mark.asyncio
async def test_probe_error_not_masked_when_close_raises() -> None:
    client = _FakeClient(
        raise_on_list=RuntimeError("list failed"),
        raise_on_close=RuntimeError("close failed"),
    )
    with pytest.raises(McpProbeError) as ei:
        await probe_remote_mcp(
            name="x",
            transport="sse",
            url="https://x.example.com/sse",
            bearer_token=None,
            timeout_s=10.0,
            client_factory=lambda c, h: client,
        )
    assert ei.value.code == "MCP_SERVER_PROBE_FAILED"


# --- leaf-cause surfacing (2026-09-08 真栈实证: 六次只看到 "ExceptionGroup") ---


def _http_401(url: str) -> httpx.HTTPStatusError:
    """Build the exact exception the SDK's streamable_http client raises on a 401."""
    req = httpx.Request("POST", url)
    return httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{url}'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401",
        request=req,
        response=httpx.Response(401, request=req),
    )


async def _probe_failing_with(exc: BaseException, **kwargs: object) -> McpProbeError:
    params: dict[str, object] = {
        "name": "deep-ai-health-mcp",
        "transport": "streamable_http",
        "url": "https://mcp.example.com/mcp",
        "bearer_token": None,
        "timeout_s": 10.0,
    }
    params.update(kwargs)
    with pytest.raises(McpProbeError) as ei:
        await probe_remote_mcp(
            client_factory=lambda c, h: _FakeClient(raise_on_start=exc),
            **params,  # type: ignore[arg-type]
        )
    return ei.value


async def test_probe_message_surfaces_exception_group_leaf() -> None:
    url = "https://mcp.example.com/mcp"
    grouped = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [_http_401(url)])

    err = await _probe_failing_with(grouped)

    assert err.code == "MCP_SERVER_PROBE_FAILED"
    assert err.message.startswith("could not connect to MCP server 'deep-ai-health-mcp': ")
    assert "HTTPStatusError: Client error '401 Unauthorized'" in err.message
    assert "ExceptionGroup" not in err.message
    # only the first line of the leaf text — the MDN link is noise
    assert "developer.mozilla.org" not in err.message


async def test_probe_message_surfaces_innermost_leaf_of_nested_groups() -> None:
    leaf = httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [leaf])])

    err = await _probe_failing_with(nested)

    assert "ConnectError: [Errno 8] nodename nor servname provided" in err.message
    assert "ExceptionGroup" not in err.message


async def test_probe_message_redacts_bearer_token_and_custom_header_values() -> None:
    token = "ghp_supersecrettoken123"
    api_key = "ck_customsecret456"
    leaky = RuntimeError(
        f"upstream rejected headers Authorization: Bearer {token}; X-Api-Key: {api_key}"
    )

    err = await _probe_failing_with(leaky, bearer_token=token, custom_headers={"X-Api-Key": api_key})

    assert token not in err.message
    assert api_key not in err.message
    assert "***" in err.message


async def test_probe_message_dedupes_leaves_and_caps_at_three() -> None:
    grouped = ExceptionGroup(
        "many",
        [
            RuntimeError("dup"),
            RuntimeError("dup"),
            ValueError("second"),
            KeyError("third"),
            OSError("fourth"),
        ],
    )

    err = await _probe_failing_with(grouped)

    assert err.message.count("RuntimeError: dup") == 1
    assert "ValueError: second" in err.message
    assert "KeyError: 'third'" in err.message
    assert "fourth" not in err.message


async def test_probe_message_truncates_long_leaf_text() -> None:
    err = await _probe_failing_with(RuntimeError("x" * 1000))

    assert len(err.message) < 400
    assert "RuntimeError: xxx" in err.message


async def test_probe_timeout_still_maps_to_probe_failed_with_type_name() -> None:
    class _SlowClient(_FakeClient):
        async def start(self) -> None:
            await asyncio.sleep(5)

    with pytest.raises(McpProbeError) as ei:
        await probe_remote_mcp(
            name="slow",
            transport="streamable_http",
            url="https://slow.example.com/mcp",
            bearer_token=None,
            timeout_s=0.01,
            client_factory=lambda c, h: _SlowClient(),
        )

    assert ei.value.code == "MCP_SERVER_PROBE_FAILED"
    # non-group + empty str(exc) → bare type name, nothing more
    assert ei.value.message == "could not connect to MCP server 'slow': TimeoutError"


async def test_probe_failure_log_carries_traceback(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="expert_work.control_plane.mcp_probe")

    await _probe_failing_with(ExceptionGroup("g", [_http_401("https://mcp.example.com/mcp")]))

    failed = [r for r in caplog.records if r.getMessage() == "mcp_probe.failed"]
    assert len(failed) == 1
    assert failed[0].exc_info is not None and failed[0].exc_info[0] is ExceptionGroup
