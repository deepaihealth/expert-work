"""``TenantStatusService`` — Stream U (PR E).

A per-tenant TTL cache over the ``tenant_config.status`` column so the auth
middleware can 403 a SUSPENDED tenant's members on every request without
hitting the DB each time. Mirrors :class:`PlatformEmbeddingConfigService`'s
store + clock + ttl pattern, but keyed per ``tenant_id`` (the suspended-set is
small and looked up by the caller's home tenant on the hot path).

A missing ``tenant_config`` row (e.g. the system tenant, or a tenant that
predates provisioning) reads as **not suspended** — fail-open here is correct
because the enforcement is a deliberate admin action, not a default-deny gate.
Write endpoints call :meth:`invalidate` for immediate effect on the writing
instance; multi-replica staleness is bounded by the TTL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.persistence.tenant_config.base import TenantConfigStore


class TenantStatusService:
    """Per-tenant ``status == "suspended"`` lookup, TTL-cached."""

    def __init__(
        self,
        *,
        store: TenantConfigStore,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        # tenant_id -> (is_suspended, expiry_ts)
        self._cache: dict[UUID, tuple[bool, float]] = {}

    async def is_suspended(self, tenant_id: UUID) -> bool:
        """``True`` iff the tenant's config row exists and ``status == "suspended"``."""
        cached = self._cache.get(tenant_id)
        if cached is not None and self._clock() < cached[1]:
            return cached[0]
        # ``tenant_config`` is FORCE-RLS and the front-door caller (the auth
        # middleware) runs *outside* ``RLSContextMiddleware``, with no tenant
        # context set yet. Scope the read to the tenant asked about: unscoped
        # it fails closed under RLS enforcement, reads "not suspended" and
        # poisons the TTL cache with that (the same trap ``run_queue_worker``
        # documents on its own ``_tenant_scope``). Caller context is restored.
        tenant = current_tenant_id_var.set(tenant_id)
        bypass = bypass_rls_var.set(False)
        try:
            row = await self._store.get(tenant_id=tenant_id)
        finally:
            bypass_rls_var.reset(bypass)
            current_tenant_id_var.reset(tenant)
        suspended = row is not None and row.status == "suspended"
        self._cache[tenant_id] = (suspended, self._clock() + self._ttl_seconds)
        return suspended

    def invalidate(self, tenant_id: UUID) -> None:
        """Drop the cached value so the next read reloads from DB."""
        self._cache.pop(tenant_id, None)
