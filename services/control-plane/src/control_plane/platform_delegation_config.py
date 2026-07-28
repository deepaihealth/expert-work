"""``PlatformDelegationConfigService`` — perf phase2 PR3.

Returns the EFFECTIVE platform delegation-gate capacity
(``max_concurrent_delegations``): the runtime DB row wins; absent a row, the
constructor-injected ``env_default`` (a frozen constant, no settings
env var). So the built-in default holds until an admin flips it in the UI,
after which the DB value wins.

Mirrors :class:`PlatformDynamicWorkerConfigService`: the resolved view is
TTL-cached; write endpoints call :meth:`invalidate` for immediate effect on
the writing instance. Multi-replica staleness is bounded by the TTL.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from expert_work.persistence.platform_delegation_config import (
    PlatformDelegationConfigStore,
)


@dataclass(frozen=True)
class DelegationConfig:
    """The platform's delegation-gate capacity (effective or configured view)."""

    max_concurrent_delegations: int


class PlatformDelegationConfigService:
    """DB-wins effective delegation-gate capacity, TTL-cached."""

    def __init__(
        self,
        *,
        store: PlatformDelegationConfigStore,
        env_default: DelegationConfig,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._env_default = env_default
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._effective = env_default
        self._configured: DelegationConfig | None = None
        self._loaded = False
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def effective(self) -> DelegationConfig:
        """The resolved capacity: DB row if configured, else ``env_default``."""
        await self._maybe_refresh()
        return self._effective

    async def configured(self) -> DelegationConfig | None:
        """The DB row value, or ``None`` when unset (→ using ``env_default``).

        Lets the API distinguish "explicitly configured" from "env default" so
        the UI can show whether a platform override is in effect.
        """
        await self._maybe_refresh()
        return self._configured

    async def put(self, *, max_concurrent_delegations: int, updated_by: str | None) -> None:
        """Upsert the singleton config row then invalidate the cache."""
        await self._store.put(
            max_concurrent_delegations=max_concurrent_delegations,
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
        # No ``bypass_rls_session()``: ``platform_delegation_config`` is a
        # tenant-less platform table with no RLS policy, exactly like
        # ``platform_tool_budget_config``.
        row = await self._store.get()
        if row is not None:
            self._configured = DelegationConfig(
                max_concurrent_delegations=row.max_concurrent_delegations,
            )
            self._effective = self._configured
        else:
            self._configured = None
            self._effective = self._env_default
        self._loaded = True
        self._expires_at = self._clock() + self._ttl_seconds
