"""Cross-replica cache-invalidation bus — Redis pub/sub (PR-E3a).

Every cache invalidation in control-plane used to be in-process only; on a
multi-replica deployment a config save on pod A left pod B serving stale
state (built-agent cache up to 1800s, MCP pools forever). This module
broadcasts invalidation events over one Redis channel so every replica
applies the same local eviction. Handlers are idempotent and run on ALL
pods, including the publisher — its local invalidation simply happens twice,
which is harmless.

Failure posture: ``publish`` NEVER raises (the caller's local invalidation
already ran; the pool/agent TTLs bound cross-replica staleness while Redis
is down), and the subscriber self-heals with capped exponential backoff — a
subscriber that dies and never recovers would silently reintroduce the whole
stale-cache bug class (the MCP pool just had exactly that disease, BUG-17).

Deployments without Redis (in-memory/dev) get :class:`NoopInvalidationBus`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from expert_work.common.observability import expert_work_counter
from expert_work.runtime.runs.schemas import RunStatus

logger = logging.getLogger("expert_work.control_plane.invalidation_bus")

#: Single fan-out channel — kinds are dispatched application-side.
CHANNEL = "expert_work:invalidation"

#: Event kinds this PR wires (documentation; publish does not gate on it).
KINDS = frozenset(
    {
        "agent_build",  # tenant-scoped built-agent eviction
        "agent_build_all",  # every tenant's built agents
        "agent_build_user",  # (tenant, user) OAuth builds
        "tenant_mcp",  # tenant MCP pool (+ agent builds, two-layer)
        "platform_mcp",  # platform MCP pool (+ all agent builds)
        "user_mcp_oauth",  # (tenant, user) OAuth pool (+ user builds)
        "tenant_config",  # tenant config cache (+ secret values, + agent builds)
        # --- PR-E3b-1: platform config plane ---
        "platform_secrets",  # credential overlay + secret values (+ builds, scoped)
        "platform_judge",  # judge config cache (+ all builds — baked in at build time)
        "platform_tool_budget",  # tool-budget cache (+ all builds — baked in at build time)
        "platform_embedding",  # embedding/rerank config cache (read per call)
        "platform_dynamic_worker",  # dynamic-worker limits cache (read per run)
        "platform_delegation",  # delegation-gate capacity cache (read per check)
        "platform_quality",  # quality sampling/judge config cache (read by workers)
        "agent_template",  # template catalog change → every tenant's builds
        "platform_skill",  # platform skill change → every tenant's builds
        "tenant_status",  # tenant suspended/active TTL cache
        "agent_disable",  # (tenant, agent) kill-switch TTL cache — whole tenant drops
        "quota_rules",  # per-tenant quota-rule cache inside the QuotaService
        "rate_limit_override",  # per-tenant rate-limit override cache (PR-D wires the service)
        # --- 跨副本取消亚秒化 ---
        "run_cancel",  # (tenant, run) a peer's cancel CAS won → the owner re-checks its lease now
    }
)

_published = expert_work_counter(
    "expert_work_control_plane_invalidation_bus_published_total",
    "Invalidation events published to the Redis bus, by event kind.",
    ["kind"],
)
_received = expert_work_counter(
    "expert_work_control_plane_invalidation_bus_received_total",
    "Invalidation events received from the Redis bus, by event kind.",
    ["kind"],
)
_errors = expert_work_counter(
    "expert_work_control_plane_invalidation_bus_errors_total",
    "Invalidation-bus failures, by stage (publish / subscribe / handler).",
    ["stage"],
)


@dataclass(frozen=True)
class InvalidationEvent:
    """One invalidation broadcast. ``origin`` (pod identifier) is informational
    for logs only — handlers must be idempotent and are applied on all pods."""

    kind: str
    tenant_id: str | None = None
    user_id: str | None = None
    origin: str = ""
    #: ``run_cancel`` only — the run whose cancel CAS just won. Serialized only
    #: when set, so every other kind keeps its exact pre-existing wire shape.
    run_id: str | None = None


Handler = Callable[[InvalidationEvent], Awaitable[None]]


class InvalidationBus:
    """Redis pub/sub publisher + self-healing subscriber."""

    def __init__(
        self,
        *,
        redis_client: Any,
        origin: str,
        channel: str = CHANNEL,
        reconnect_initial_s: float = 0.5,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self._redis = redis_client
        self._origin = origin
        self._channel = channel
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._task: asyncio.Task[None] | None = None
        # publish_soon fire-and-forget tasks — referenced here so the event
        # loop cannot GC them mid-flight.
        self._pending: set[asyncio.Task[None]] = set()

    async def publish(self, event: InvalidationEvent) -> None:
        """Broadcast ``event``. NEVER raises — Redis-down degrades to a
        WARNING + counter; the caller's local invalidation already happened
        and the cache TTLs are the cross-replica fallback."""
        body: dict[str, Any] = {
            "kind": event.kind,
            "tenant_id": event.tenant_id,
            "user_id": event.user_id,
        }
        if event.run_id is not None:
            body["run_id"] = event.run_id
        body["origin"] = event.origin or self._origin
        payload = json.dumps(body)
        try:
            await self._redis.publish(self._channel, payload)
        except Exception:
            _errors.labels(stage="publish").inc()
            logger.warning("invalidation_bus.publish_failed kind=%s", event.kind, exc_info=True)
            return
        _published.labels(kind=event.kind).inc()

    def publish_soon(self, event: InvalidationEvent) -> None:
        """Sync-context publish for the sync funnels (skill promotion /
        rollback gates, OAuth token refresh) that hold plain callables.
        Fire-and-forget on the running loop; never raises."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _errors.labels(stage="publish").inc()
            logger.warning("invalidation_bus.publish_soon_no_loop kind=%s", event.kind)
            return
        task = loop.create_task(self.publish(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def run_subscriber(self, handlers: Mapping[str, Handler]) -> None:
        """Long-lived subscribe/dispatch loop. Reconnects forever with capped
        exponential backoff; only cancellation (shutdown) exits."""
        backoff = self._reconnect_initial_s
        while True:
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(self._channel)
                logger.info("invalidation_bus.subscribed channel=%s", self._channel)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    backoff = self._reconnect_initial_s
                    await self._dispatch(message.get("data"), handlers)
            except asyncio.CancelledError:
                raise
            except Exception:
                _errors.labels(stage="subscribe").inc()
                logger.warning(
                    "invalidation_bus.subscriber_error reconnect_in=%.1fs",
                    backoff,
                    exc_info=True,
                )
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max_s)

    async def _dispatch(self, raw: Any, handlers: Mapping[str, Handler]) -> None:
        try:
            data = json.loads(raw)
            event = InvalidationEvent(
                kind=str(data["kind"]),
                tenant_id=data.get("tenant_id"),
                user_id=data.get("user_id"),
                origin=str(data.get("origin") or ""),
                run_id=data.get("run_id"),
            )
        except Exception:
            _errors.labels(stage="subscribe").inc()
            logger.warning("invalidation_bus.bad_payload")
            return
        _received.labels(kind=event.kind).inc()
        handler = handlers.get(event.kind)
        if handler is None:
            logger.warning("invalidation_bus.unknown_kind kind=%s", event.kind)
            return
        try:
            await handler(event)
        except Exception:
            # Log + continue — one bad handler must never kill the loop.
            _errors.labels(stage="handler").inc()
            logger.exception(
                "invalidation_bus.handler_failed kind=%s origin=%s", event.kind, event.origin
            )

    def start(self, handlers: Mapping[str, Handler]) -> None:
        """Start the subscriber task (idempotent while running)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self.run_subscriber(handlers))

    async def stop(self) -> None:
        """Cancel + await the subscriber and any in-flight publishes."""
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        if self._task is not None:
            self._task.cancel()
            # ``gather`` rather than a bare ``await self._task``: it swallows
            # the CancelledError the cancel just raised, and reads as an
            # effectful call (CodeQL flags bare-name awaits as no-op statements).
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


class NoopInvalidationBus:
    """Bus for deployments without Redis (in-memory/dev, unit tests).

    Cross-replica invalidation is meaningless there — every surface is inert,
    with a one-time INFO log so an operator can tell the bus is off."""

    def __init__(self) -> None:
        self._logged = False

    def _log_once(self) -> None:
        if not self._logged:
            self._logged = True
            logger.info(
                "invalidation_bus.noop no Redis configured; "
                "cross-replica cache invalidation disabled"
            )

    async def publish(self, event: InvalidationEvent) -> None:
        """No-op (single-process deployment)."""
        self._log_once()

    def publish_soon(self, event: InvalidationEvent) -> None:
        """No-op (single-process deployment)."""
        self._log_once()

    def start(self, handlers: Mapping[str, Handler]) -> None:
        """No-op — nothing to subscribe to."""
        self._log_once()

    async def stop(self) -> None:
        """No-op — no subscriber task exists."""


def build_invalidation_handlers(state: Any) -> dict[str, Handler]:
    """Handlers per event kind over the caches hanging off ``app.state``.

    Two-layer invariant (design): tenant MCP pools / tenant config are baked
    into ``BuiltAgent`` at build time, so every handler that invalidates an
    inner layer ALSO drops the agent-build layer for the same scope — encoded
    here once, not at publish sites. ``AgentRuntime.invalidate_*`` fans out to
    the sub-agent build cache via its registered hooks, so the delegation
    layer is covered too. All lookups are late (``getattr`` at event time) so
    partially-wired apps (tests, injected runtimes) degrade to a no-op.
    """

    async def _agent_build(event: InvalidationEvent) -> None:
        runtime = getattr(state, "agent_runtime", None)
        if runtime is not None and event.tenant_id is not None:
            runtime.invalidate_tenant(UUID(event.tenant_id))

    async def _agent_build_all(event: InvalidationEvent) -> None:
        """``event`` carries no scope — the whole build cache drops."""
        runtime = getattr(state, "agent_runtime", None)
        if runtime is not None:
            runtime.invalidate_all()

    async def _agent_build_user(event: InvalidationEvent) -> None:
        runtime = getattr(state, "agent_runtime", None)
        if runtime is not None and event.tenant_id is not None and event.user_id is not None:
            runtime.invalidate_user(UUID(event.tenant_id), event.user_id)

    async def _tenant_mcp(event: InvalidationEvent) -> None:
        if event.tenant_id is None:
            return
        pool = getattr(state, "tenant_mcp_pool_service", None)
        if pool is not None:
            await pool.invalidate(UUID(event.tenant_id))
        await _agent_build(event)

    async def _platform_mcp(event: InvalidationEvent) -> None:
        pool = getattr(state, "platform_mcp_pool_service", None)
        if pool is not None:
            await pool.invalidate()
        await _agent_build_all(event)

    async def _user_mcp_oauth(event: InvalidationEvent) -> None:
        if event.tenant_id is None or event.user_id is None:
            return
        pool = getattr(state, "user_mcp_oauth_pool_service", None)
        if pool is not None:
            await pool.invalidate(UUID(event.tenant_id), event.user_id)
        await _agent_build_user(event)

    async def _tenant_config(event: InvalidationEvent) -> None:
        if event.tenant_id is None:
            return
        service = getattr(state, "tenant_config_service", None)
        if service is not None:
            service.invalidate(UUID(event.tenant_id))
        # A config PUT can swap ``model_credentials_ref`` — the resolved
        # plaintext values (300s CredentialValueCache) must drop with it.
        cache = getattr(state, "credential_value_cache", None)
        if cache is not None:
            cache.invalidate_tenant(UUID(event.tenant_id))
        await _agent_build(event)

    async def _platform_secrets(event: InvalidationEvent) -> None:
        """Platform credential overlay: resolved-ref cache + plaintext value
        cache + built agents (keys are baked in at build time). Tenant-scoped
        events (tenant override endpoints) drop only that tenant's scope."""
        service = getattr(state, "platform_secrets_service", None)
        if service is not None:
            service.invalidate()
        cache = getattr(state, "credential_value_cache", None)
        if event.tenant_id is not None:
            if cache is not None:
                cache.invalidate_tenant(UUID(event.tenant_id))
            await _agent_build(event)
        else:
            if cache is not None:
                cache.invalidate_all()
            await _agent_build_all(event)

    def _platform_config(attr: str, *, rebuild: bool) -> Handler:
        """Handler over one of the TTL-cached singleton platform config
        services (sync no-arg ``invalidate``). ``rebuild=True`` for configs
        baked into ``BuiltAgent`` at build time (judge / tool budget) — those
        must ALSO drop every build; the rest are re-read at call/run time."""

        async def _handler(event: InvalidationEvent) -> None:
            service = getattr(state, attr, None)
            if service is not None:
                service.invalidate()
            if rebuild:
                await _agent_build_all(event)

        return _handler

    async def _tenant_status(event: InvalidationEvent) -> None:
        if event.tenant_id is None:
            return
        service = getattr(state, "tenant_status_service", None)
        if service is not None:
            service.invalidate(UUID(event.tenant_id))

    async def _agent_disable(event: InvalidationEvent) -> None:
        # The event carries no agent name — the cache is small, the whole
        # tenant's entries drop.
        if event.tenant_id is None:
            return
        service = getattr(state, "agent_disable_service", None)
        if service is not None:
            service.invalidate_tenant(UUID(event.tenant_id))

    async def _quota_rules(event: InvalidationEvent) -> None:
        if event.tenant_id is None:
            return
        service = getattr(state, "quota_service", None)
        if service is not None:
            service.invalidate_tenant(UUID(event.tenant_id))

    async def _rate_limit_override(event: InvalidationEvent) -> None:
        # ``app.state.tenant_rate_limit_overrides`` arrives with PR-D (the
        # limiter-middleware rework); until then the getattr is always None
        # and this handler is a deliberate no-op.
        if event.tenant_id is None:
            return
        service = getattr(state, "tenant_rate_limit_overrides", None)
        if service is not None:
            service.invalidate(UUID(event.tenant_id))

    async def _run_cancel(event: InvalidationEvent) -> None:
        """跨副本取消亚秒化 —— 一条 ``run_cancel`` = 「某个副本对这个 run 的
        ``request_cancel`` CAS 赢了」。属主副本不等 ``_heartbeat_loop`` 下一次
        续租(``lease_ttl_s / 3``,生产 10s),现在就做**同一个**心跳 CAS:
        租约没了(行已 interrupted)→ 置位 ``abort_event``,与周期心跳丢租约
        时一模一样的中断路径;租约还在(消息来路不明 / 乱序)→ 只是多续了
        一次租,什么都不停 —— 真相在 durable 行,不在消息里。

        幂等:不是本副本的 run / 租户不符 / 已 abort / 已终态 → 静默;PENDING
        留给 → RUNNING 的守卫(洞 A)处理,不另开一条路。
        """
        runtime = getattr(state, "agent_runtime", None)
        if runtime is None or event.run_id is None or event.tenant_id is None:
            return
        run_manager = runtime.run_manager
        run_id = UUID(event.run_id)
        record = run_manager.get(run_id)
        if (
            record is None
            or record.tenant_id != UUID(event.tenant_id)
            or record.status is not RunStatus.RUNNING
            or record.abort_event.is_set()
        ):
            return
        if await run_manager.heartbeat(run_id):
            return
        logger.info("invalidation_bus.run_cancel.abort run_id=%s origin=%s", run_id, event.origin)
        record.abort_event.set()

    return {
        "agent_build": _agent_build,
        "agent_build_all": _agent_build_all,
        "agent_build_user": _agent_build_user,
        "tenant_mcp": _tenant_mcp,
        "platform_mcp": _platform_mcp,
        "user_mcp_oauth": _user_mcp_oauth,
        "tenant_config": _tenant_config,
        "platform_secrets": _platform_secrets,
        "platform_judge": _platform_config("platform_judge_config_service", rebuild=True),
        "platform_tool_budget": _platform_config(
            "platform_tool_budget_config_service", rebuild=True
        ),
        "platform_embedding": _platform_config("platform_embedding_config_service", rebuild=False),
        "platform_dynamic_worker": _platform_config(
            "platform_dynamic_worker_config_service", rebuild=False
        ),
        "platform_delegation": _platform_config(
            "platform_delegation_config_service", rebuild=False
        ),
        "platform_quality": _platform_config("quality_config_service", rebuild=False),
        # No inner cache of their own — the content is read from the store at
        # build time, so the build layer IS the cache.
        "agent_template": _agent_build_all,
        "platform_skill": _agent_build_all,
        "tenant_status": _tenant_status,
        "agent_disable": _agent_disable,
        "quota_rules": _quota_rules,
        "rate_limit_override": _rate_limit_override,
        "run_cancel": _run_cancel,
    }
