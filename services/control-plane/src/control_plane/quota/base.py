"""Abstract ``QuotaService`` Protocol — Stream C.5.

Defines the check / reserve / commit / release surface that the
control plane exposes both in-process (for handler admission) and
over the internal HTTP API (``/v1/quota/*``). Two implementations
land alongside:

* :class:`control_plane.quota.in_memory.InMemoryQuotaService` —
  fully in-process; used by unit tests and the dev default when no
  Redis DSN is configured.
* :class:`control_plane.quota.redis_quota.RedisQuotaService` — the
  production engine. Hot-path bucket counters live in Redis (Lua
  atomic eval); reservation rows + monthly ledger live in Postgres
  (RLS-scoped).

Both implementations consume the same :class:`TenantQuotaStore` /
:class:`TokenReservationStore` Protocols from
:mod:`expert_work.persistence.quota`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from expert_work.protocol import (
    CheckRequest,
    CheckResult,
    CommitRequest,
    ReserveRequest,
    ReserveResult,
)


class BudgetExceededError(Exception):
    """Reservation denied because the monthly token budget is exhausted."""


@runtime_checkable
class QuotaService(Protocol):
    """Check / reserve / commit / release the per-tenant quota state."""

    async def check(self, req: CheckRequest) -> CheckResult:
        """Allow / deny a request against the per-tenant rate-limit dimensions."""

    async def reserve_tokens(self, req: ReserveRequest) -> ReserveResult:
        """Reserve ``estimated_tokens`` against the monthly budget."""

    async def commit_tokens(self, req: CommitRequest) -> None:
        """Finalise a reservation with actual usage; refund the over-estimate."""

    async def release_tokens(self, reservation_id: UUID, *, tenant_id: UUID) -> None:
        """Cancel a reservation; refund all reserved tokens."""

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        """Drop the tenant's cached quota rules so the next check reloads
        from the store (quota-admin writes / invalidation-bus events)."""
