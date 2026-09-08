"""In-process LRU secret cache — keyed by ``(tenant_id, secret_ref)``.

Keeps SecretStore read QPS down (subsystems/11 § 5.1). M0 uses a flat
TTL: the proxy resolves refs through :class:`SecretStore`, which returns
a bare value with no rotation metadata, so static / dynamic TTL
differentiation is deferred with the real KMS backend.

Never shared across tenants — the key includes ``tenant_id``.
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


class SecretCache:
    """A bounded LRU of resolved secret values with a flat TTL."""

    def __init__(
        self,
        *,
        max_size: int,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()

    def get(self, key: CacheKey) -> str | None:
        """Return the cached value, or ``None`` on a miss / expired entry."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.value

    def put(self, key: CacheKey, value: str) -> None:
        """Cache ``value`` under ``key``, evicting the LRU entry if full."""
        self._entries[key] = _Entry(value=value, expires_at=self._clock() + self._ttl_s)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def invalidate_all(self) -> None:
        """Drop every cached secret — ``/admin/cache/invalidate`` and the
        unscoped invalidation-bus events (B-31 ①)."""
        self._entries.clear()

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        """Drop every cached secret of one tenant — the tenant-scoped
        invalidation-bus events (B-31 ①). The key already carries the
        tenant, so this is a filter, not a second index."""
        for key in [k for k in self._entries if k[0] == tenant_id]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
