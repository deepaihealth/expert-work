"""Per-run dynamic-worker spawn budget — 1.3 Orchestrator-Worker.

A leaf module (no imports from ``orchestrator.tools.registry``) so both
``registry`` (which references the type on :class:`ToolContext`) and
``spawn_worker`` (which constructs + consumes it) can import it without
forming an import cycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from expert_work.common.observability import expert_work_counter

logger = logging.getLogger(__name__)


@dataclass
class WorkerSpawnBudget:
    """Per-run spawn budget — a cumulative count cap + a concurrency gate.

    Created once per run (in ``sse.run_agent``) from the platform settings
    and threaded through :class:`~orchestrator.tools.registry.ToolContext` so
    every ``spawn_worker`` call in the run shares it. ``max_per_run`` bounds
    total spawns across all turns; the semaphore bounds how many workers run
    at once.
    """

    max_per_run: int
    max_concurrent: int
    _spawned: int = 0
    _sem: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.max_concurrent)

    def try_reserve(self) -> bool:
        """Count one spawn against the per-run cap; ``False`` if exhausted."""
        if self._spawned >= self.max_per_run:
            return False
        self._spawned += 1
        return True

    @asynccontextmanager
    async def concurrency(self) -> AsyncIterator[None]:
        async with self._sem:
            yield


DELEGATION_GATE_KEY: Final = "delegation_gate"

DELEGATIONS_GATED = expert_work_counter(
    "expert_work_delegations_gated_total",
    "Delegations refused by the global concurrency gate (acquire timeout).",
)


# 二期 PR3(spec P4)— 进程级委托并发闸。容量每次 acquire 时经
# capacity_provider 现读(provider 内部是配置服务的内存 TTL 读,默认
# 分钟级以内),配置热生效语义 = 对下一次委托生效,不影响已在闸内的。
# 单进程部署下真闸得住;HA 双色同活时每实例一闸(与本仓多副本 TTL
# 兜底同一立场)。
class DelegationGate:
    """Process-wide concurrency gate for delegations (subagent + spawn_worker).

    ``acquire`` waits up to ``timeout_s`` for a slot; returns False on
    timeout (caller degrades to a soft-fail ToolResult — never raises, so a
    depth-1 delegation holding all slots cannot deadlock its own depth-2).

    Fix round 1 (queue-head blocking): ``capacity_provider`` is invoked
    OUTSIDE ``self._cond`` — each waiting acquirer re-reads capacity, then
    briefly takes the lock only to compare + increment. The production
    provider (``PlatformDelegationConfigService.effective``) is an in-memory
    TTL-cached read (default well under a minute) that does one real DB
    round-trip whenever the cache expires, so running it under the lock
    would make every ``acquire`` *and every ``release``* (release also needs
    the lock) queue behind that one slow DB round-trip — a queue-head-
    blocking amplifier turning one slow query into a platform-wide stall.
    The provider's latency is still bounded: it runs inside the same
    ``asyncio.timeout(self._timeout_s)`` that bounds the whole wait, so a
    hung provider still degrades to a timed-out ``acquire`` rather than
    hanging forever. ``release`` never calls the provider and is therefore
    never blocked by it, regardless of how slow the provider is.

    If the provider raises (including a provider-internal ``TimeoutError``,
    e.g. an asyncpg query timeout), ``acquire`` fails OPEN: it falls back to
    the last successfully read capacity, or — if the provider has never
    succeeded — treats the gate as unbounded for that call. This is a
    latency-protection gate, not a security gate, so an unreadable config is
    treated the same direction as ``delegation_gate=None`` (ungated) rather
    than blocking delegations on a config-store hiccup.
    """

    def __init__(
        self,
        capacity_provider: Callable[[], Awaitable[int]],
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self._capacity_provider = capacity_provider
        self._timeout_s = timeout_s
        self._active = 0
        self._cond = asyncio.Condition()
        self._last_capacity: int | None = None

    @property
    def active(self) -> int:
        return self._active

    async def acquire(self) -> bool:
        try:
            async with asyncio.timeout(self._timeout_s):
                while True:
                    capacity = await self._read_capacity()
                    async with self._cond:
                        if capacity is None or self._active < capacity:
                            self._active += 1
                            return True
                        await self._cond.wait()
        except TimeoutError:
            return False

    async def _read_capacity(self) -> int | None:
        """Read capacity from the provider, outside ``self._cond``.

        Returns ``None`` to mean "unbounded" (fail-open) when the provider
        has raised and has never returned successfully. On a provider
        exception after at least one success, returns the last known-good
        capacity instead of failing open — a config store that was reading
        fine and then started erroring shouldn't suddenly uncap the gate.
        """
        try:
            capacity = max(1, int(await self._capacity_provider()))
        except Exception:
            # Catches provider-internal errors, including a bare built-in
            # ``TimeoutError`` (e.g. an asyncpg query timeout) — caught HERE
            # so it fails open instead of propagating to the ``except
            # TimeoutError`` in ``acquire``, where it would be
            # indistinguishable from — and miscounted as — the gate being
            # saturated. ``asyncio.CancelledError`` (raised when the outer
            # ``asyncio.timeout`` itself expires while awaiting the
            # provider) is a ``BaseException``, not caught here, and
            # propagates to become that outer timeout as intended.
            logger.warning(
                "DelegationGate capacity_provider failed; falling back to last known capacity %r",
                self._last_capacity,
                exc_info=True,
            )
            return self._last_capacity
        self._last_capacity = capacity
        return capacity

    async def release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()
