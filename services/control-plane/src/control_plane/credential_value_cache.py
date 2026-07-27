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
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from expert_work.runtime.secret_store import SecretStore

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


# repr=False: the synthesized dataclass repr would recurse into ``inner``
# (e.g. a dev secret store whose repr shows its plaintext mapping) — a log
# line or traceback rendering this object must never leak secret values.
@dataclass(frozen=True, repr=False)
class CachingSecretStore:
    """Tenant-scoped :class:`SecretStore` adapter over a
    :class:`CredentialValueCache` — PR2 T3.

    aux_model_adapter / quality_judge have no direct ``secret_store.get``;
    their vault read happens inside ``build_llm_router``
    (agent_factory.py:1956). Handing the router this wrapper — built per
    call with the caller's ``tenant_id`` — turns that read into a cache
    hit without touching the orchestrator factory. Only latest-version
    reads are cached: the cache key carries no ``version`` dimension, so
    caching a pinned read would cross versions both ways (a pinned read
    could serve a cached latest value, a latest read a cached pinned
    one) — a pinned ``version`` therefore bypasses the cache entirely.
    Writes / deletes pass through, then evict this tenant's cached
    entries so a follow-up read never serves the pre-write value.

    Cache keys use the bare secret *name* ``build_llm_router`` passes
    (post-``parse_secret_ref``); the Resolving classes key on the full
    ``secret://`` ref. Same values, disjoint keys — both are swept by the
    tenant/all invalidation T2 wired in.
    """

    inner: SecretStore
    cache: CredentialValueCache
    tenant_id: UUID

    async def get(self, name: str, *, version: str | None = None) -> str:
        if version is not None:
            return await self.inner.get(name, version=version)
        hit = self.cache.get(self.tenant_id, name)
        if hit is not None:
            return hit
        value = await self.inner.get(name)
        self.cache.put(self.tenant_id, name, value)
        return value

    async def put(self, name: str, value: str) -> None:
        await self.inner.put(name, value)
        # Evict after the write so a read through this wrapper can't serve
        # the pre-write value for up to a TTL. Tenant-wide (the cache has no
        # per-key invalidate) — writes are rare, the sweep is cheap.
        self.cache.invalidate_tenant(self.tenant_id)

    async def list_versions(self, name: str) -> list[str]:
        return await self.inner.list_versions(name)

    async def delete(self, name: str) -> None:
        await self.inner.delete(name)
        self.cache.invalidate_tenant(self.tenant_id)
