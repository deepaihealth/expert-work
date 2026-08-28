"""In-memory :class:`PlatformDynamicWorkerConfigStore` — B3 PR2."""

from __future__ import annotations

import asyncio

from expert_work.persistence.platform_dynamic_worker_config.base import (
    PlatformDynamicWorkerConfigRow,
    PlatformDynamicWorkerConfigStore,
)


class InMemoryPlatformDynamicWorkerConfigStore(PlatformDynamicWorkerConfigStore):
    """Holds a single optional row; lock-guarded for asyncio safety."""

    def __init__(self) -> None:
        self._row: PlatformDynamicWorkerConfigRow | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> PlatformDynamicWorkerConfigRow | None:
        async with self._lock:
            return self._row

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
        async with self._lock:
            self._row = PlatformDynamicWorkerConfigRow(
                max_concurrent=max_concurrent,
                max_per_run=max_per_run,
                max_iterations=max_iterations,
                cap_max_concurrent=cap_max_concurrent,
                cap_max_per_run=cap_max_per_run,
                cap_max_iterations=cap_max_iterations,
                updated_by=updated_by,
            )
