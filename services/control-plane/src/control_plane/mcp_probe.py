"""Connect-probe for tenant-registered remote MCP servers — Stream V-C.

Before a remote MCP server is persisted (or its url/token changed), the
control plane connects to it and calls ``list_tools`` to prove it is real and
reachable. The probe runs the orchestrator's remote MCP client in-process
(control-plane already depends on the orchestrator). The URL is SSRF-checked
at this boundary too — never trust that the caller validated it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, runtime_checkable

from expert_work.common.url_validation import RemoteURLError, validate_remote_url
from orchestrator.tools.mcp import (
    MCPServerConfig,
    MCPToolDef,
    SseMCPClient,
    StreamableHttpMCPClient,
)

logger = logging.getLogger("expert_work.control_plane.mcp_probe")


@runtime_checkable
class _ProbeClient(Protocol):
    async def start(self) -> None:
        """Open the transport connection + MCP session."""

    async def list_tools(self) -> Sequence[MCPToolDef]:
        """Return the tools the server advertises."""

    async def close(self) -> None:
        """Tear the connection down."""


# A factory so tests can inject a fake client. Production builds the real
# transport client from config + already-resolved headers.
ProbeClientFactory = Callable[[MCPServerConfig, Mapping[str, str]], _ProbeClient]


class McpProbeError(Exception):
    """Probe failed. ``code`` is the machine-readable API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _default_client_factory(config: MCPServerConfig, headers: Mapping[str, str]) -> _ProbeClient:
    if config.transport == "sse":
        return SseMCPClient(config=config, resolved_headers=dict(headers))
    return StreamableHttpMCPClient(config=config, resolved_headers=dict(headers))


# The SDK's streamable_http/sse clients run inside an anyio task group, so the
# real cause (httpx 401 / DNS gaierror / ConnectError) reaches us wrapped in an
# ``ExceptionGroup``. ``type(exc).__name__`` alone told the admin nothing
# (2026-09-08: six attempts, six times "ExceptionGroup").
_MAX_LEAF_CAUSES = 3
_MAX_LEAF_TEXT = 200
_REDACTED = "***"


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten (possibly nested) exception groups into their leaf exceptions, in order."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaf_exceptions(sub)]
    return [exc]


def _summarize_leaf(exc: BaseException) -> str:
    """``TypeName: first line of str(exc)`` — bare ``TypeName`` when str(exc) is empty."""
    text = str(exc).strip()
    first_line = text.splitlines()[0].strip() if text else ""
    summary = f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
    return summary[:_MAX_LEAF_TEXT]


def _redact(text: str, secrets: Sequence[str]) -> str:
    # Longest first so "Bearer <token>" collapses to one "***" instead of "Bearer ***".
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return text


def _describe_failure(exc: BaseException, secrets: Sequence[str]) -> str:
    """Leaf-cause summary for the API message: deduped, capped, secret-free."""
    summaries: list[str] = []
    for leaf in _leaf_exceptions(exc):
        summary = _redact(_summarize_leaf(leaf), secrets)
        if summary not in summaries:
            summaries.append(summary)
        if len(summaries) == _MAX_LEAF_CAUSES:
            break
    return "; ".join(summaries)


async def probe_remote_mcp(
    *,
    name: str,
    transport: str,
    url: str,
    bearer_token: str | None,
    timeout_s: float,
    custom_headers: Mapping[str, str] | None = None,
    sse_read_timeout_s: float | None = None,
    client_factory: ProbeClientFactory = _default_client_factory,
) -> Sequence[MCPToolDef]:
    """Connect to a remote MCP server and return its advertised tools.

    Raises :class:`McpProbeError` (with ``code`` in
    ``{MCP_SERVER_INVALID_URL, MCP_SERVER_PROBE_FAILED}``) on SSRF rejection,
    connect failure, timeout, or list_tools error. Never logs the token.

    ``custom_headers`` (M1) are merged in BEFORE the bearer ``Authorization`` so
    the probe exercises the exact header set the runtime will send, with bearer
    always winning — matching ``_build_mcp_client``.

    Each phase (start, list_tools) gets the full timeout_s; total wall time is
    at most 2x timeout_s.
    """
    try:
        validate_remote_url(url)
    except RemoteURLError as exc:
        raise McpProbeError("MCP_SERVER_INVALID_URL", str(exc)) from exc

    headers: dict[str, str] = {}
    if custom_headers:
        headers.update({str(k): str(v) for k, v in custom_headers.items()})
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"

    # MCPServerConfig.__post_init__ requires auth_config["token_ref"] when
    # auth_type="bearer". We supply the sentinel "secret://probe" — the actual
    # token is injected via resolved_headers above; the config field never
    # carries the real secret (it is repr=False regardless). If bearer_token is
    # None we use auth_type="none" so no sentinel is needed.
    config = MCPServerConfig(
        name=name,
        transport=transport,  # type: ignore[arg-type]
        url=url,
        auth_type="bearer" if bearer_token is not None else "none",
        auth_config={"token_ref": "secret://probe"} if bearer_token is not None else {},
        timeout_s=timeout_s,
        sse_read_timeout_s=sse_read_timeout_s,
    )
    client: _ProbeClient = client_factory(config, headers)
    try:
        await asyncio.wait_for(client.start(), timeout=timeout_s)
        tools: Sequence[MCPToolDef] = await asyncio.wait_for(client.list_tools(), timeout=timeout_s)
        return tools
    except Exception as exc:  # probe maps all failures (incl. TimeoutError) to McpProbeError
        # NB: do not put the tenant-supplied server name/url/transport in the log
        # message — CodeQL py/log-injection flags request-derived values; the
        # caller surfaces the failure (with context) to the API response + audit
        # already. exc_info carries the full chain for pod-log debugging.
        logger.warning("mcp_probe.failed", exc_info=True)
        # Exception text is never expected to echo header values, but the
        # message goes to the tenant UI — strip them if it ever does.
        secrets = [*headers.values()]
        if bearer_token is not None:
            secrets.append(bearer_token)
        raise McpProbeError(
            "MCP_SERVER_PROBE_FAILED",
            f"could not connect to MCP server {name!r}: {_describe_failure(exc, secrets)}",
        ) from exc
    finally:
        try:
            await client.close()
        except Exception:  # best-effort teardown; close errors must not mask probe errors
            logger.warning("mcp_probe.close_failed")
