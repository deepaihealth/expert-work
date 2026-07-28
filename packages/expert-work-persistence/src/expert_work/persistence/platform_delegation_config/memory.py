"""In-memory :class:`PlatformDelegationConfigStore` — perf phase2 PR3."""

from __future__ import annotations

import asyncio

from expert_work.persistence.platform_delegation_config.base import (
    PlatformDelegationConfigRow,
    PlatformDelegationConfigStore,
)


class InMemoryPlatformDelegationConfigStore(PlatformDelegationConfigStore):
    """Holds a single optional row; lock-guarded for asyncio safety."""

    def __init__(self) -> None:
        self._row: PlatformDelegationConfigRow | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> PlatformDelegationConfigRow | None:
        async with self._lock:
            return self._row

    async def put(self, *, max_concurrent_delegations: int, updated_by: str | None) -> None:
        async with self._lock:
            self._row = PlatformDelegationConfigRow(
                max_concurrent_delegations=max_concurrent_delegations,
                updated_by=updated_by,
            )
