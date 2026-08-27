"""``AgentDisableService`` — Stream RT-4 (RT-ADR-16, kill switch).

A per-(tenant, agent_name) TTL cache over the ``agent_disable.disabled`` flag so
the admission / session-resolve / queue-claim gates can reject a disabled
agent's traffic on every request without hitting the DB each time. Mirrors
:class:`~control_plane.tenant_status.TenantStatusService`'s store + clock + ttl
pattern, but keyed per ``(tenant_id, agent_name)`` (the disabled set is small and
looked up by the agent the caller is targeting on the hot path).

A missing ``agent_disable`` row reads as **not disabled** — fail-open here is
correct because the enforcement is a deliberate admin action, not a
default-deny gate. The disable/enable endpoints call :meth:`invalidate` for
immediate effect on the writing instance; multi-replica staleness is bounded by
the TTL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from expert_work.persistence.agent_disable.base import AgentDisableStore


class AgentDisableService:
    """Per-(tenant, agent) ``disabled`` lookup, TTL-cached."""

    def __init__(
        self,
        *,
        store: AgentDisableStore,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        # (tenant_id, agent_name) -> (is_disabled, expiry_ts)
        self._cache: dict[tuple[UUID, str], tuple[bool, float]] = {}

    async def is_disabled(self, tenant_id: UUID, agent_name: str) -> bool:
        """``True`` iff the agent's row exists and ``disabled`` is set."""
        key = (tenant_id, agent_name)
        cached = self._cache.get(key)
        if cached is not None and self._clock() < cached[1]:
            return cached[0]
        row = await self._store.get(tenant_id=tenant_id, agent_name=agent_name)
        disabled = row is not None and row.disabled
        self._cache[key] = (disabled, self._clock() + self._ttl_seconds)
        return disabled

    def invalidate(self, tenant_id: UUID, agent_name: str) -> None:
        """Drop the cached value so the next read reloads from DB."""
        self._cache.pop((tenant_id, agent_name), None)

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        """Drop every cached flag for ``tenant_id`` — the invalidation-bus
        ``agent_disable`` event carries no agent name, and the cache is small
        enough that a whole-tenant sweep is the simpler correct move."""
        for key in [k for k in self._cache if k[0] == tenant_id]:
            del self._cache[key]
