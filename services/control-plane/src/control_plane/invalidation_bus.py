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
        "tenant_config",  # tenant config cache (+ agent builds)
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
        payload = json.dumps(
            {
                "kind": event.kind,
                "tenant_id": event.tenant_id,
                "user_id": event.user_id,
                "origin": event.origin or self._origin,
            }
        )
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
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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
        await _agent_build(event)

    return {
        "agent_build": _agent_build,
        "agent_build_all": _agent_build_all,
        "agent_build_user": _agent_build_user,
        "tenant_mcp": _tenant_mcp,
        "platform_mcp": _platform_mcp,
        "user_mcp_oauth": _user_mcp_oauth,
        "tenant_config": _tenant_config,
    }
