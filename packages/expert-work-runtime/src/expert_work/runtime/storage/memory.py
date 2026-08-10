"""``InMemoryObjectStore`` — process-local store for unit tests / dev."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from expert_work.runtime.storage.base import (
    LockMode,
    ObjectLockedError,
    ObjectNotFoundError,
    validate_lock_args,
)


@dataclass(frozen=True)
class _StoredObject:
    """Internal record. Kept frozen to enforce the immutability rule."""

    data: bytes
    content_type: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    # D.1b: software lock state. ``None``/``None`` means the object is
    # not locked. The pair always moves together.
    retain_until: datetime | None = None
    lock_mode: LockMode | None = None


class InMemoryObjectStore:
    """Thread-safe in-memory store; intended for unit tests and dev fakes."""

    def __init__(self) -> None:
        self._objects: dict[str, _StoredObject] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        retain_until: datetime | None = None,
        lock_mode: LockMode | None = None,
    ) -> None:
        validate_lock_args(retain_until, lock_mode)
        record = _StoredObject(
            data=data,
            content_type=content_type,
            metadata=dict(metadata) if metadata else {},
            retain_until=retain_until,
            lock_mode=lock_mode,
        )
        async with self._lock:
            existing = self._objects.get(key)
            if existing is not None and _is_still_locked(existing):
                msg = (
                    f"object {key!r} is under {existing.lock_mode} retention "
                    f"until {existing.retain_until!r}; cannot overwrite"
                )
                raise ObjectLockedError(msg)
            self._objects[key] = record

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        buf = bytearray()
        async for chunk in chunks:
            buf.extend(chunk)
        # 委托 put:锁检查 / 覆盖语义天然同契约。
        await self.put(key, bytes(buf), content_type=content_type)

    async def get(self, key: str) -> bytes:
        record = self._objects.get(key)
        if record is None:
            msg = f"object not found: {key!r}"
            raise ObjectNotFoundError(msg)
        return record.data

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._objects.pop(key, None)

    async def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))

    async def presigned_url(
        self,
        key: str,
        *,
        expires_in: int = 3600,
        method: Literal["GET", "PUT"] = "GET",
    ) -> str:
        """Return a deterministic fake URL — there is no real signing here.

        Sufficient for tests that need *some* URL; production code paths
        must hit :class:`S3CompatibleObjectStore`.
        """
        return f"memory://{key}?method={method}&expires_in={expires_in}"


def _is_still_locked(existing: _StoredObject) -> bool:
    """Return True iff ``existing`` is compliance-locked past ``now``.

    Governance mode is permissive by design — privileged callers may
    overwrite — so we only block re-puts when the prior record was
    written in compliance mode and its retention hasn't elapsed yet.
    """
    if existing.lock_mode != "compliance" or existing.retain_until is None:
        return False
    return existing.retain_until > datetime.now(tz=UTC)
