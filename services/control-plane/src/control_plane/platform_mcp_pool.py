"""Platform-configured MCP server pool — Stream MCP platform-servers (P1b).

The platform-curated catalog (``mcp_connector_catalog``, NULL-tenant) now holds
fully-configured **shared** MCP servers (P1a): ``none`` / ``bearer`` entries the
platform admin set up once (a ``bearer`` entry carries the platform's own token
via ``bearer_token_ref``). This service builds those rows into a single,
process-global :class:`MCPServerPool` that the agent builder layers at platform
precedence (above tenant + per-user pools), gated per-tenant by
``tenant_config.mcp_allowlist`` exactly like the operator file pool.

``oauth2`` catalog entries are **not** here — they are per-user and flow through
``user_mcp_oauth_pool`` after each user authorizes.

The pool is rebuilt lazily. A catalog change reaches only the entry it
concerns (B-259: :meth:`server_changed` retires it, :meth:`server_removed`
stops it, the other entries' clients are reused by the next build);
:meth:`invalidate` (legacy whole-pool event) and :meth:`close_all` (shutdown)
close everything. Catalog rows are NULL-tenant, so the store call
runs inside ``bypass_rls_session()`` (the W-8 trap — a scoped session hides
them). Decoupling mirrors ``tenant_mcp_pool``: the orchestrator never imports
this; the agent builder receives a ``Callable`` provider bound to the service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from control_plane.mcp_kept_clients import KeptClient, KeptMcpClients
from control_plane.mcp_retired_clients import (
    RetiredMcpClients,
    close_retired,
    release_clients,
    retire_in_place,
)
from control_plane.tenant_scope import bypass_rls_session
from expert_work.persistence import McpConnectorCatalogStore
from expert_work.protocol import McpConnectorCatalogRecord
from orchestrator.tools.mcp import (
    MCPCallResult,
    MCPClient,
    MCPServerConfig,
    MCPServerPool,
    MCPServerPoolLimitError,
    MCPToolDef,
)

logger = logging.getLogger("expert_work.control_plane.platform_mcp_pool")

# Bounded rebuild attempts when invalidation keeps landing mid-build.
_MAX_BUILD_ATTEMPTS = 5

# TTL backstop for missed invalidation-bus events (PR-E3a) — matches the
# built-agent cache TTL (AgentRuntime.cache_ttl_s = 1800s). Without it the
# pool lives forever, so one lost bus event would be permanent staleness.
_POOL_TTL_S = 1800.0

# The platform pool has a single cache entry; its retired clients share one key.
_RETIRED_KEY = "platform"

# Provider handed to the agent builder: () -> the platform pool.
PlatformMcpPoolProvider = Callable[[], Awaitable[MCPServerPool]]

# Factory so tests can inject a RecordingMCPClient instead of real transports.
McpClientFactory = Callable[[MCPServerConfig], Awaitable[MCPClient]]


@dataclass
class _ToolFilteringClient:
    """Wraps an :class:`MCPClient`, dropping platform-disabled tools from
    ``list_tools`` so they are never registered / exposed to a tenant agent.

    Satisfies the runtime-checkable ``MCPClient`` protocol; ``call_tool`` /
    ``close`` / ``retire`` pass straight through to the wrapped transport.
    """

    inner: MCPClient
    disabled: frozenset[str]

    async def list_tools(self) -> Sequence[MCPToolDef]:
        return [t for t in await self.inner.list_tools() if t.name not in self.disabled]

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> MCPCallResult:
        return await self.inner.call_tool(name, args)

    async def close(self) -> None:
        await self.inner.close()

    async def retire(self) -> None:
        # Pool TTL expiry: retire the wrapped remote client so holders keep
        # working; a transport without ``retire`` is closed instead.
        retire = getattr(self.inner, "retire", None)
        await (retire() if retire is not None else self.inner.close())


def _record_to_config(record: McpConnectorCatalogRecord) -> MCPServerConfig:
    """Map a catalog row to an orchestrator :class:`MCPServerConfig`.

    ``bearer`` carries the platform token's ``token_ref`` so the client builder
    resolves it via the SecretStore (the value never lives on the config). The
    catalog ``url_template`` is a concrete URL in the platform-server model.
    """
    auth_config: dict[str, str] = {}
    if record.auth_type == "bearer" and record.bearer_token_ref is not None:
        auth_config["token_ref"] = record.bearer_token_ref
    # Runtime tuning (NULL = orchestrator defaults). Only override timeout_s when
    # the row carries one, so MCPServerConfig's own default still applies.
    extra: dict[str, float] = {}
    if record.timeout_s is not None:
        extra["timeout_s"] = record.timeout_s
    return MCPServerConfig(
        name=record.name,
        transport=record.transport,
        url=record.url_template,
        auth_type=record.auth_type,
        auth_config=auth_config,
        sse_read_timeout_s=record.sse_read_timeout_s,
        **extra,
    )


def _is_shared_server(record: McpConnectorCatalogRecord) -> bool:
    """A catalog row that belongs in the shared platform pool.

    Enabled + ``none``/``bearer`` (oauth2 is per-user). A ``bearer`` entry must
    carry a platform ``bearer_token_ref`` — a legacy tenant-fills bearer (one
    ``auth_schema`` secret field, no platform token) is *not* a shared server
    and is skipped here (it instantiates per-tenant the old way until P2).
    """
    if not record.enabled:
        return False
    if record.auth_type == "none":
        return True
    if record.auth_type == "bearer":
        return record.bearer_token_ref is not None
    return False  # oauth2 → user_mcp_oauth_pool


def connection_fingerprint(record: McpConnectorCatalogRecord) -> object:
    """B-259 —— 池里这一项的客户端由什么决定(连哪、怎么认证、超时、滤掉哪些工具)。
    指纹相同 = 同一个连接,可以原样复用;和 ``_record_to_config`` 同源,不会漂移。"""
    return (_record_to_config(record), frozenset(record.disabled_tools))


#: 目录改动对运行期的影响,由轻到重(B-259)。
CatalogChange = Literal["none", "builds", "changed", "removed"]

#: B-259 —— 运行期不读的字段:只给人看的,和只在租户新开通时校验的档位(已开通
#: 的构建不再查它)。其余字段一律当作会影响运行,新加的字段默认也算。
_NO_RUNTIME_EFFECT = frozenset(
    {
        "display_name",
        "description",
        "category",
        "icon",
        "required_tier",
        "id",
        "created_at",
        "updated_at",
        "updated_by",
    }
)


def classify_catalog_change(
    before: McpConnectorCatalogRecord | None,
    after: McpConnectorCatalogRecord | None,
    *,
    secret_rewritten: bool = False,
) -> CatalogChange:
    """B-259 —— 一次目录新建 / 修改 / 删除(``None`` 表示不存在)对平台池的影响:

    * ``removed`` —— 这一项离开平台池(停用 / 删除 / 不再是共享服务):只硬停它;
    * ``changed`` —— 新进平台池,或连接指纹变了,或令牌原地重贴(引用不变、值变了):
      只退役它;
    * ``builds`` —— 平台池不受影响,但别的运行期字段变了(oauth2 条目等):只清构建;
    * ``none`` —— 只改了展示类字段:什么都不动。
    """
    was = before is not None and _is_shared_server(before)
    now = after is not None and _is_shared_server(after)
    if was and not now:
        return "removed"
    if now and after is not None:
        if before is None or not was or secret_rewritten:
            return "changed"
        if connection_fingerprint(before) != connection_fingerprint(after):
            return "changed"
    if before is None or after is None:
        return "builds"
    ignored = set(_NO_RUNTIME_EFFECT)
    if before.model_dump(exclude=ignored) != after.model_dump(exclude=ignored):
        return "builds"
    return "none"


def classify_catalog_delete(existing: McpConnectorCatalogRecord) -> CatalogChange:
    """B-259 —— 删除前读的 ``existing`` 不在行锁下:并发的重新启用可能已把它建进池。
    none / bearer 一律按 ``removed``(多停一次空的无害,幂等),oauth2 只清构建。"""
    if existing.auth_type in ("none", "bearer"):
        return "removed"
    return classify_catalog_change(existing, None)


class PlatformMcpPoolService:
    """Caches the single process-global platform MCP server pool."""

    def __init__(
        self,
        *,
        store: McpConnectorCatalogStore,
        client_factory: McpClientFactory,
        clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._client_factory = client_factory
        self._clock = clock  # injectable monotonic time (tests)
        # B-248 — called once the TTL retired the pool: the app evicts every
        # cached agent build (they hold its clients), so only runs already in
        # flight are left on the retired clients' one-shot sessions.
        self._on_expire = on_expire
        # (pool, built_at monotonic stamp for the TTL backstop)
        self._pool: tuple[MCPServerPool, float] | None = None
        self._guard = asyncio.Lock()  # guards _pool + _generation + _retired
        # Clients retired on TTL expiry that runs may still hold (weak): a
        # later ``invalidate`` / ``close_all`` must hard-stop them too.
        self._retired: RetiredMcpClients[str] = RetiredMcpClients()
        self._build_lock = asyncio.Lock()  # dedups concurrent builds
        self._generation = 0
        # B-259 —— 缓存池成员的连接指纹;改一项时其它项暂存在这里给下次构建复用。
        # Guarded by ``_guard``.
        self._kept: KeptMcpClients[str] = KeptMcpClients()

    async def get_or_build(self) -> MCPServerPool:
        """Return the platform pool, building (and caching) on miss.

        A server that fails to connect is skipped (logged, no row-derived values)
        so one bad server cannot break the agent build. When the server cap is
        hit the just-opened client is closed before breaking. A generation
        counter guards the lost-invalidation race (parity with tenant pool):
        ``invalidate`` bumps it under ``_guard`` while a build may be in flight;
        the build captures it before building and re-checks before caching, and
        retries (bounded) if it changed.
        """
        for attempt in range(_MAX_BUILD_ATTEMPTS):
            last_attempt = attempt == _MAX_BUILD_ATTEMPTS - 1
            async with self._build_lock:
                cached = self._pool
                if cached is not None:
                    cached_pool, built_at = cached
                    if self._clock() - built_at < _POOL_TTL_S:
                        return cached_pool
                    # TTL backstop (PR-E3a): expired — drop like ``invalidate``
                    # (the generation bump keeps any concurrent build from
                    # caching stale data), then rebuild. Retire, not close:
                    # cached agents and in-flight runs still hold these
                    # clients and keep calling them on one-shot sessions.
                    async with self._guard:
                        self._generation += 1
                        self._pool = None
                        self._retired.add_pool(_RETIRED_KEY, cached_pool)
                        self._kept.dropped(_RETIRED_KEY)
                    try:
                        await cached_pool.retire_all()
                    except Exception:
                        logger.warning("platform_mcp_pool.expired_close_failed")
                    self._notify_expired()
                async with self._guard:
                    gen = self._generation
                    carried = self._kept.peek(_RETIRED_KEY)
                pool, members, fresh = await self._build(carried)
                unused: list[MCPClient] = []
                async with self._guard:
                    cached_now = self._generation == gen
                    if cached_now:
                        for server, client in self._kept.cached(_RETIRED_KEY, members):
                            self._retired.add(_RETIRED_KEY, server, client)
                            unused.append(client)
                        # 复用的长连接按它最早的出生时间算 TTL,兜底时限不被拉长。
                        born = [m.born_at for m in members.values()]
                        self._pool = (pool, min([*born, self._clock()]))
                    elif last_attempt:
                        # B-248 — no cache entry will ever close it: track it
                        # for invalidate / close_all, retire it below.
                        self._retired.add_pool(_RETIRED_KEY, pool)
                        self._kept.adopted(_RETIRED_KEY, members)
            if cached_now:
                # B-259 —— 暂存里没被复用的(指纹变了 / 这一项没了):退役。
                await release_clients(retire=unused)
                return pool
            if last_attempt:
                await retire_in_place(pool)  # outside the build lock (B-248)
                return pool  # serve fresh-but-uncached; next caller rebuilds
            # Invalidation landed mid-build: close what this attempt opened and
            # retry. B-259 —— 复用来的暂存客户端不关:别的构建 / run 还拿着它们。
            await release_clients(close=fresh)
        raise RuntimeError("get_or_build: retry loop exited without returning")  # pragma: no cover

    async def _build(
        self, carried: dict[str, KeptClient]
    ) -> tuple[MCPServerPool, dict[str, KeptClient], list[MCPClient]]:
        """Build a pool from the catalog; returns it, its members and the
        clients this call opened (as opposed to reused from ``carried``)."""
        pool = MCPServerPool()
        members: dict[str, KeptClient] = {}
        fresh: list[MCPClient] = []
        async with bypass_rls_session():
            records = await self._store.list()
        for record in records:
            if not _is_shared_server(record):
                continue
            fingerprint = connection_fingerprint(record)
            kept = carried.get(record.name)
            if kept is not None and kept.fingerprint != fingerprint:
                kept = None
            try:
                if kept is not None:
                    # B-259 —— 连接配置没变:复用暂存的长连接,不重连。
                    client, born_at = kept.client, kept.born_at
                else:
                    client = await self._client_factory(_record_to_config(record))
                    if record.disabled_tools:
                        # Platform-curated tool denylist — drop disabled tools
                        # from list_tools so no tenant agent ever registers them.
                        client = _ToolFilteringClient(
                            inner=client, disabled=frozenset(record.disabled_tools)
                        )
                    born_at = self._clock()
                await pool.add(record.name, client)
                members[record.name] = KeptClient(client, fingerprint, born_at)
                if kept is None:
                    fresh.append(client)
            except MCPServerPoolLimitError:
                if kept is None:
                    try:
                        await client.close()
                    except Exception:
                        logger.warning("platform_mcp_pool.cap_orphan_close_failed")
                logger.warning("platform_mcp_pool.server_cap_reached")
                break
            except Exception:
                logger.warning("platform_mcp_pool.server_build_failed")
        return pool, members, fresh

    def _notify_expired(self) -> None:
        """Run ``on_expire`` (build eviction); a failure never fails the caller."""
        if self._on_expire is None:
            return
        try:
            self._on_expire()
        except Exception as exc:
            logger.warning("platform_mcp_pool.on_expire_failed err=%s", type(exc).__name__)

    async def server_changed(self, name: str) -> None:
        """B-259 —— 目录项 ``name`` 新加 / 改了连接配置 / 重新启用:只退役它自己的客户端
        (正在跑的调用跑完;之后的调用走一次性会话、仍是旧配置,直到持有它的构建被清掉),
        池里其它项的长连接留给下一次构建复用。"""
        await self._switch(name, stop=False)

    async def server_removed(self, name: str) -> None:
        """B-259 —— 目录项 ``name`` 被停用 / 删除:只硬停它自己(连同 TTL 先前退役的
        副本),其它项照常服务。"""
        await self._switch(name, stop=True)

    async def _switch(self, name: str, *, stop: bool) -> None:
        async with self._guard:
            # 正在进行的构建可能是按旧目录读的:让它重来。
            self._generation += 1
            self._pool = None
            hit = self._kept.switch(_RETIRED_KEY, name)
            target = [hit.client] if hit is not None else []
            if stop:
                target += self._retired.pop_one(_RETIRED_KEY, name)
            elif hit is not None:
                self._retired.add(_RETIRED_KEY, name, hit.client)
        # 先清构建,再拆连接:新 run 不会在拆的过程中拿到旧构建(B-248 同序)。
        self._notify_expired()
        if stop:
            await release_clients(close=target)
        else:
            await release_clients(retire=target)

    async def invalidate(self) -> None:
        """Close + drop the cached pool (next build rebuilds from the catalog),
        and hard-stop clients an earlier TTL expiry retired."""
        async with self._guard:
            self._generation += 1
            entry = self._pool
            self._pool = None
            retired = self._retired.pop_all() + self._kept.pop_key(_RETIRED_KEY)
        if entry is not None:
            try:
                await entry[0].close_all()
            except Exception:
                logger.warning("platform_mcp_pool.invalidate_close_failed")
        await close_retired(retired)

    async def close_all(self) -> None:
        """Close the cached pool and every retired client (app shutdown)."""
        async with self._guard:
            self._generation += 1
            entry = self._pool
            self._pool = None
            retired = self._retired.pop_all() + self._kept.pop_all()
        if entry is not None:
            try:
                await entry[0].close_all()
            except Exception:
                logger.warning("platform_mcp_pool.close_all_failed")
        await close_retired(retired)
