"""Abstract ``UserWorkspaceStore`` repository — Stream J.15.

Implementations:
- :class:`expert_work.persistence.workspace.memory.InMemoryUserWorkspaceStore`
- :class:`expert_work.persistence.workspace.sql.SqlUserWorkspaceStore`
"""

from __future__ import annotations

import abc
from datetime import datetime
from uuid import UUID

from expert_work.protocol import UserWorkspace


def workspace_volume_name(tenant_id: UUID, user_id: UUID) -> str:
    """Return the docker named-volume identifier for a ``(tenant, user)`` pair.

    Deterministic — the same pair always maps to the same volume, so a
    ``resolve()`` after a row already exists never has to reconcile a
    name. The id components are plain UUIDs (no secret), and an
    ``expert-work-ws-`` prefix makes the volume self-describing in
    ``docker volume ls``.
    """
    return f"expert-work-ws-{tenant_id}-{user_id}"


class WorkspaceNotFoundError(KeyError):
    """Raised when an op targets a ``user_workspace`` row that doesn't exist."""


class UserWorkspaceStore(abc.ABC):
    """Per-user persistent-workspace registry, scoped to ``(tenant_id, user_id)``.

    Supervisor-owned — there is no RLS on ``user_workspace``; the tenant
    and user are passed explicitly and scoping is application-layer
    (Mini-ADR J-1).
    """

    @abc.abstractmethod
    async def resolve(self, *, tenant_id: UUID, user_id: UUID) -> UserWorkspace:
        """Return the workspace for ``(tenant_id, user_id)``, creating it if absent.

        Idempotent upsert keyed by ``(tenant_id, user_id)``. The
        ``volume_name`` is deterministic (:func:`workspace_volume_name`),
        so a repeat call never changes it; ``last_accessed_at`` is
        bumped to *now* on every call.

        Soft-deleted rows (``deleted_at IS NOT NULL``) are still returned
        — soft-delete enforcement is a supervisor-layer concern. Callers
        must check ``workspace.deleted_at`` before acting on the row.
        ``last_accessed_at`` is **not** bumped for soft-deleted rows
        (resolve becomes a pure read).
        """

    @abc.abstractmethod
    async def get(self, *, tenant_id: UUID, user_id: UUID) -> UserWorkspace | None:
        """Read-only lookup for ``(tenant_id, user_id)`` — ``None`` if absent.

        Unlike :meth:`resolve` this never creates a row and never bumps
        ``last_accessed_at``. Used by read surfaces (e.g. the playground
        workspace inspector) that must observe whether a workspace really
        exists without provisioning one as a side effect.
        """

    @abc.abstractmethod
    async def update_size(self, *, workspace_id: UUID, size_bytes: int) -> None:
        """Set ``size_bytes`` to the latest measurement (Mini-ADR J-29 第 1 项).

        Called by :class:`QuotaEnforcer.refresh_size` after a fresh
        ``du`` inside the mounted container. The supervisor's
        ``release()`` runs this fire-and-forget so the exec hot path
        isn't blocked. ``size_bytes`` must be ``>= 0``; pass an int.

        Raises :class:`WorkspaceNotFoundError` if ``workspace_id``
        doesn't exist.
        """

    @abc.abstractmethod
    async def add_size(self, *, workspace_id: UUID, delta_bytes: int) -> None:
        """Atomically add ``delta_bytes`` to ``size_bytes``, flooring at 0.

        沙箱迁移波 3 (spec § 3.2 记账第 1 层) — 上传成功后增量入账用。
        与 :meth:`update_size`(绝对值覆写,扫描/重算用)互补。负数下限 0
        是防御性语义,两实现必须一致(SQL ``GREATEST``,memory ``max``)。
        """

    @abc.abstractmethod
    async def soft_delete(self, *, workspace_id: UUID, now: datetime) -> None:
        """Mark a workspace soft-deleted (Mini-ADR J-36 lifecycle 第 2 档).

        Sets ``deleted_at = now``. Idempotent: a second soft_delete is
        a no-op (keeps the original timestamp). The reaper picks the
        row up on its next sweep, runs the archive job, then calls
        :meth:`mark_archived` to advance the lifecycle.

        Raises :class:`WorkspaceNotFoundError` if ``workspace_id``
        doesn't exist.
        """

    @abc.abstractmethod
    async def mark_archived(self, *, workspace_id: UUID, archived_object_key: str) -> None:
        """Record the ObjectStore key of the tar.zst archive (Mini-ADR J-36 第 3 档).

        Called by the reaper after the archive job uploads the tar.zst
        and physically removes the docker volume. The row stays in the
        table until hard-delete (90 days, retention-cleanup-job — J.15-
        补强-2). Idempotent.

        The row must already be soft-deleted (CHECK constraint
        ``user_workspace_archive_consistency``); the SQL layer rejects
        archiving an active row at the DB level. Caller responsibility
        to soft_delete first.

        Raises :class:`WorkspaceNotFoundError` if ``workspace_id``
        doesn't exist.
        """

    @abc.abstractmethod
    async def list_pending_archive(self) -> list[UserWorkspace]:
        """Return soft-deleted workspaces whose archive job hasn't run yet.

        Equivalent SQL filter:
        ``deleted_at IS NOT NULL AND archived_object_key IS NULL``.
        Backed by the partial index
        ``user_workspace_pending_archive_idx`` for constant-time scans
        as the active table grows.
        """

    @abc.abstractmethod
    async def list_active(self) -> list[UserWorkspace]:
        """Return every active (not soft-deleted) workspace.

        Stream J.15-补强-2 — the daily backup sweep iterates this list
        and snapshots each volume to ObjectStore. M0 reads the whole
        list (single supervisor, single tenant block); scalable
        pagination can come with the multi-host migration (推 M1).
        """

    @abc.abstractmethod
    async def list_archived_expired(
        self, *, before: datetime, limit: int = 100
    ) -> list[UserWorkspace]:
        """Return archived workspaces whose retention window has elapsed.

        Equivalent SQL filter:
        ``deleted_at IS NOT NULL AND deleted_at < before AND
        archived_object_key IS NOT NULL``, ordered by ``deleted_at``
        ascending. Rows still pending archive (``archived_object_key IS
        NULL``) are excluded even if old — the retention sweep only
        ever hard-deletes rows whose archive has already landed in
        ObjectStore.

        Phase 3b (90 天物理清除) 地基 — consumed by the retention pass
        (Task 7).
        """

    @abc.abstractmethod
    async def hard_delete(self, *, workspace_id: UUID) -> bool:
        """Physically remove a ``user_workspace`` row.

        Returns ``True`` if a row was deleted, ``False`` if
        ``workspace_id`` didn't exist. Idempotent: a second call after
        a successful delete returns ``False`` rather than raising.

        Phase 3b 地基 — the retention pass calls this only after the
        row's ``archived_object_key`` has already been confirmed
        durable in ObjectStore (see :meth:`list_archived_expired`).
        """
