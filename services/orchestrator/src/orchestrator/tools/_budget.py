"""Per-run dynamic-worker spawn budget — 1.3 Orchestrator-Worker.

A leaf module (no imports from ``orchestrator.tools.registry``) so both
``registry`` (which references the type on :class:`ToolContext`) and
``spawn_worker`` (which constructs + consumes it) can import it without
forming an import cycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from expert_work.common.observability import expert_work_counter


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

_delegations_gated = expert_work_counter(
    "expert_work_delegations_gated_total",
    "Delegations refused by the global concurrency gate (acquire timeout).",
)


# 二期 PR3(spec P4)— 进程级委托并发闸。容量每次 acquire 时经
# capacity_provider 现读(provider 内部是 30s TTL 的配置服务),配置
# 热生效语义 = 对下一次委托生效,不影响已在闸内的。单进程部署下
# 真闸得住;HA 双色同活时每实例一闸(与本仓多副本 TTL 兜底同一立场)。
class DelegationGate:
    """Process-wide concurrency gate for delegations (subagent + spawn_worker).

    ``acquire`` waits up to ``timeout_s`` for a slot; returns False on
    timeout (caller degrades to a soft-fail ToolResult — never raises, so a
    depth-1 delegation holding all slots cannot deadlock its own depth-2).
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

    @property
    def active(self) -> int:
        return self._active

    async def acquire(self) -> bool:
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._cond:
                    while True:
                        capacity = max(1, int(await self._capacity_provider()))
                        if self._active < capacity:
                            self._active += 1
                            return True
                        await self._cond.wait()
        except TimeoutError:
            return False

    async def release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()
