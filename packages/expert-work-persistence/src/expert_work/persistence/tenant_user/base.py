"""Abstract ``TenantUserStore`` repository — Stream J.14.

Implementations:
- :class:`expert_work.persistence.tenant_user.memory.InMemoryTenantUserStore`
- :class:`expert_work.persistence.tenant_user.sql.SqlTenantUserStore`
"""

from __future__ import annotations

import abc
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from expert_work.protocol import SubjectType, TenantUser


class TenantUserStore(abc.ABC):
    """Per-user registry repository.

    Every method takes ``tenant_id`` explicitly — the tenant is the hard
    isolation boundary. ``user_id`` (the surrogate ``TenantUser.id``) is
    an application-layer ownership scope.
    """

    @abc.abstractmethod
    async def resolve(
        self,
        *,
        tenant_id: UUID,
        subject_type: SubjectType,
        subject_id: str,
        display_name: str | None = None,
    ) -> TenantUser:
        """Return the registry row for this principal, creating it if absent.

        Idempotent upsert keyed by ``(tenant_id, subject_type,
        subject_id)``. ``last_active_at`` is bumped to *now* on every
        call; ``display_name`` overwrites the stored value only when a
        non-``None`` value is supplied.
        """

    @abc.abstractmethod
    async def get(self, user_id: UUID, *, tenant_id: UUID) -> TenantUser | None:
        """Read a user by surrogate id, filtered to ``tenant_id``.

        Returns ``None`` when the row does not exist or belongs to a
        different tenant — never reveals cross-tenant existence.
        """

    @abc.abstractmethod
    async def get_by_subject(
        self, *, tenant_id: UUID, subject_type: SubjectType, subject_id: str
    ) -> TenantUser | None:
        """Point-lookup by identity, or ``None`` when absent — never creates.

        The read-only counterpart of :meth:`resolve` (which is a mint-on-use
        upsert). Hits the ``tenant_user_identity_uniq`` index directly, so it
        stays O(1) regardless of how many users the tenant has — the external
        read plane needs this because a third-party app mints one row per end
        user and can easily exceed any list page.

        ``deleted_at`` semantics: same as :meth:`get`, NOT :meth:`list_by_tenant`
        — a soft-deactivated (purged) row is still returned, not hidden. This
        keeps the two surrogate-vs-natural-key lookups consistent with each
        other (both are "read this exact row" primitives; only the *listing*
        method curates a roster). Callers that need "purged users are
        invisible" behavior (matching :meth:`list_by_tenant`) must check
        ``deleted_at`` themselves — this method does not decide that for them.
        """

    @abc.abstractmethod
    async def get_many(
        self, user_ids: Collection[UUID], *, tenant_id: UUID
    ) -> dict[UUID, TenantUser]:
        """Batch :meth:`get` — one read for the M2 users rollup.

        Returns a map keyed by ``TenantUser.id``; ids that don't exist or
        belong to a different tenant are simply absent (same non-disclosure
        semantics as :meth:`get`). An empty ``user_ids`` returns ``{}``.
        """

    @abc.abstractmethod
    async def list_by_tenant(
        self,
        tenant_id: UUID,
        *,
        subject_type: SubjectType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TenantUser]:
        """List a tenant's registry rows, most-recently-active first.

        The user-dimension observability view (Phase 2) enumerates every
        principal that has acted in the tenant. ``subject_type`` narrows to
        one kind — the view passes ``"user"`` to list the people who used an
        agent (both API-supplied end-users and logged-in employees) and
        exclude service accounts. Ordered by ``last_active_at`` desc, then
        ``created_at`` desc for a stable tiebreak.

        Phase 3a — soft-deactivated rows (``deleted_at IS NOT NULL``) are
        excluded: a purged user never reappears in the roster.
        """

    @abc.abstractmethod
    async def deactivate(self, user_id: UUID, *, tenant_id: UUID, now: datetime) -> bool:
        """Soft-deactivate a user — stamp ``deleted_at`` (Phase 3a purge_user).

        Idempotent: a second call on an already-deactivated user is a safe
        no-op that still returns ``True`` (the row exists). Returns ``False``
        only when no such row exists in ``tenant_id`` (never reveals
        cross-tenant existence). Tenant-scoped — never touches another
        tenant's row. The row is KEPT (owned data may still be recoverable
        within the retention window); ``resolve`` leaves ``deleted_at`` as-is
        so a returning purged identity stays deactivated.
        """

    @abc.abstractmethod
    async def hard_delete_deactivated(self, *, before: datetime, limit: int = 1000) -> int:
        """PR1 Task 5 — retention job: physically DELETE deactivated rows
        whose ``deleted_at`` is older than ``before``.

        Cross-tenant: NO ``tenant_id`` predicate — mirrors
        :meth:`expert_work.persistence.memory.base.MemoryStore.hard_delete_expired`,
        this is the platform-wide retention sweep's own method (consumed by
        the ``retention-cleanup-job``), not a per-tenant repository call.
        Deletes at most ``limit`` rows, oldest ``deleted_at`` first, and
        returns the number of rows actually removed. Active rows
        (``deleted_at IS NULL``) — including one that re-``resolve``d after
        being deactivated — are never touched.
        """
