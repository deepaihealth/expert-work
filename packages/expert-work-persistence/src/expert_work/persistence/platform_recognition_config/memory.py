"""In-memory :class:`PlatformRecognitionConfigStore` — B-209."""

from __future__ import annotations

import asyncio

from expert_work.persistence.platform_recognition_config.base import (
    PlatformRecognitionConfigRecord,
    PlatformRecognitionConfigStore,
)


class InMemoryPlatformRecognitionConfigStore(PlatformRecognitionConfigStore):
    """Holds a single optional row; lock-guarded for asyncio safety."""

    def __init__(self) -> None:
        self._row: PlatformRecognitionConfigRecord | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> PlatformRecognitionConfigRecord | None:
        async with self._lock:
            return self._row

    async def put(self, record: PlatformRecognitionConfigRecord) -> None:
        async with self._lock:
            self._row = record
