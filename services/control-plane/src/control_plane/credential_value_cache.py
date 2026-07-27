"""In-process LRU+TTL cache for resolved secret values.

Same shape as credential-proxy's ``SecretCache`` (keyed by
``(tenant_id, secret_ref)``, bounded LRU, flat TTL, lazy expiry on read).
Process-local: with multiple replicas, staleness after a credential change
is bounded by the TTL — this repo's accepted stance.

Methods are synchronous and unlocked on purpose: no I/O happens inside,
and all callers run on a single asyncio event loop, so operations never
interleave mid-call. Do not share an instance across threads.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

CacheKey = tuple[UUID, str]


@dataclass(frozen=True)
class _Entry:
    value: str
    expires_at: float


class CredentialValueCache:
    """A bounded LRU of resolved secret values with a flat TTL."""

    def __init__(
        self,
        *,
        max_size: int = 256,
        ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()

    def get(self, tenant_id: UUID, secret_ref: str) -> str | None:
        """Return the cached value, or ``None`` on a miss / expired entry."""
        key: CacheKey = (tenant_id, secret_ref)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.value

    def put(self, tenant_id: UUID, secret_ref: str, value: str) -> None:
        """Cache ``value``, evicting the LRU entry if full."""
        key: CacheKey = (tenant_id, secret_ref)
        self._entries[key] = _Entry(value=value, expires_at=self._clock() + self._ttl_s)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def invalidate_all(self) -> None:
        """Drop every cached value — platform-level credential changes."""
        self._entries.clear()

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        """Drop this tenant's cached values — tenant-override changes."""
        for key in [k for k in self._entries if k[0] == tenant_id]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
