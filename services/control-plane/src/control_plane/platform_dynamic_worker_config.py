"""``PlatformDynamicWorkerConfigService`` — B3 PR2.

Returns the EFFECTIVE platform ``dynamic_worker`` limits (``max_concurrent``,
``max_per_run``, ``max_iterations``): the runtime DB row wins; absent a row,
the constructor-injected ``env_default`` (the process's frozen settings
snapshot). So the env stays the bootstrap default / ops hard-revert until an
admin flips it in the UI, after which the DB value wins.

Mirrors :class:`PlatformToolBudgetConfigService`: the resolved view is
TTL-cached; write endpoints call :meth:`invalidate` for immediate effect on
the writing instance. Multi-replica staleness is bounded by the TTL.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from expert_work.persistence.platform_dynamic_worker_config import (
    PlatformDynamicWorkerConfigStore,
)


@dataclass(frozen=True)
class DynamicWorkerConfig:
    """The platform's dynamic-worker limits (effective or configured view).

    弹性 worker 预算(2026-08-28)— two tiers: ``max_*`` is the platform
    *default* an agent gets when its manifest does not ask; ``cap_max_*`` is
    the hard ceiling a per-agent ``dynamic_workers.max_*`` request is clamped
    to at run time.
    """

    max_concurrent: int
    max_per_run: int
    max_iterations: int
    cap_max_concurrent: int
    cap_max_per_run: int
    cap_max_iterations: int


class PlatformDynamicWorkerConfigService:
    """DB-wins effective dynamic-worker limits, TTL-cached."""

    def __init__(
        self,
        *,
        store: PlatformDynamicWorkerConfigStore,
        env_default: DynamicWorkerConfig,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._env_default = env_default
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._effective = env_default
        self._configured: DynamicWorkerConfig | None = None
        self._loaded = False
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def effective(self) -> DynamicWorkerConfig:
        """The resolved limits: DB row if configured, else ``env_default``."""
        await self._maybe_refresh()
        return self._effective

    async def configured(self) -> DynamicWorkerConfig | None:
        """The DB row value, or ``None`` when unset (→ using ``env_default``).

        Lets the API distinguish "explicitly configured" from "env default" so
        the UI can show whether a platform override is in effect.
        """
        await self._maybe_refresh()
        return self._configured

    async def put(
        self,
        *,
        max_concurrent: int,
        max_per_run: int,
        max_iterations: int,
        cap_max_concurrent: int,
        cap_max_per_run: int,
        cap_max_iterations: int,
        updated_by: str | None,
    ) -> None:
        """Upsert the singleton config row then invalidate the cache."""
        await self._store.put(
            max_concurrent=max_concurrent,
            max_per_run=max_per_run,
            max_iterations=max_iterations,
            cap_max_concurrent=cap_max_concurrent,
            cap_max_per_run=cap_max_per_run,
            cap_max_iterations=cap_max_iterations,
            updated_by=updated_by,
        )
        self.invalidate()

    def invalidate(self) -> None:
        """Drop the cache so the next read reloads from DB."""
        self._expires_at = 0.0

    async def _maybe_refresh(self) -> None:
        if self._loaded and self._clock() < self._expires_at:
            return
        async with self._lock:
            if self._loaded and self._clock() < self._expires_at:
                return
            await self._reload()

    async def _reload(self) -> None:
        # No ``bypass_rls_session()``: ``platform_dynamic_worker_config`` is a
        # tenant-less platform table with no RLS policy, exactly like
        # ``platform_tool_budget_config``.
        row = await self._store.get()
        if row is not None:
            self._configured = DynamicWorkerConfig(
                max_concurrent=row.max_concurrent,
                max_per_run=row.max_per_run,
                max_iterations=row.max_iterations,
                cap_max_concurrent=row.cap_max_concurrent,
                cap_max_per_run=row.cap_max_per_run,
                cap_max_iterations=row.cap_max_iterations,
            )
            self._effective = self._configured
        else:
            self._configured = None
            self._effective = self._env_default
        self._loaded = True
        self._expires_at = self._clock() + self._ttl_seconds
