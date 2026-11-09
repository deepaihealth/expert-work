"""Abstract ``ImageUploadStore`` repository — Stream J.6.补强-3 (Mini-ADR J-32).

Implementations:

* :class:`expert_work.persistence.image_upload.memory.InMemoryImageUploadStore`
* :class:`expert_work.persistence.image_upload.sql.SqlImageUploadStore`

The store is scoped by ``tenant_id`` at the application layer; the SQL
implementation also applies a tenant RLS policy (migration 0028) so a
forgotten WHERE clause cannot cross-leak.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from expert_work.protocol import ImageUpload


class ImageUploadNotFoundError(KeyError):
    """Raised when an op targets an ``image_upload`` row that doesn't exist."""


class ImageUploadStore(abc.ABC):
    """Per-tenant registry of landed image uploads.

    Two outward-facing read shapes: ``get`` (one row by id) and
    ``list_active`` (rows for a thread / tenant where ``deleted_at IS
    NULL``). The retention sweep uses :meth:`list_reapable` to find rows
    past their tenant's retention window (or already soft-deleted) +
    :meth:`hard_delete` to remove them once the object-store key is
    cleared.
    """

    @abc.abstractmethod
    async def insert(
        self,
        *,
        image_id: UUID,
        tenant_id: UUID,
        thread_id: UUID,
        user_id: UUID | None,
        object_key: str,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> ImageUpload:
        """Persist one row for a successful upload.

        Returns the materialised row including the server-assigned
        ``created_at`` so the caller can echo it back in the API
        response without a re-read.
        """

    @abc.abstractmethod
    async def get(self, *, image_id: UUID, tenant_id: UUID) -> ImageUpload | None:
        """Return the row by id, or ``None`` when the id is unknown or
        belongs to a different tenant (404-equivalent — never raise on
        cross-tenant probes)."""

    @abc.abstractmethod
    async def soft_delete(self, *, image_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        """Flip ``deleted_at`` for an active row; return ``False`` when
        the id is unknown, already soft-deleted, or in another tenant
        (caller turns that into 404 — same hides-cross-tenant rule)."""

    @abc.abstractmethod
    async def list_active_for_thread(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
    ) -> list[ImageUpload]:
        """Active rows in a thread — used by tests + the J.6 image listing
        path. Sorted by ``created_at`` descending."""

    @abc.abstractmethod
    async def list_reapable(
        self,
        *,
        now: datetime,
        default_days: int,
        limit: int = 1000,
    ) -> list[ImageUpload]:
        """Rows the retention sweep may remove: past their **last use** OR
        ``deleted_at IS NOT NULL``. Sorted by ``created_at`` ascending.

        B-283 —— 最后使用 = ``greatest(created_at, last_used_at, 所在会话最后一次 run 的
        created_at)``(只认同租户的 run);到期 = 最后使用 + 保留期 ``< now``,保留期取这张图
        租户的 ``tenant_config.upload_retention_days``(图片也是上传),为空(或租户没有配置行)
        时用 ``default_days``。同样的天数下只会比按 ``created_at`` 算留得更久,不会更短。

        ANY soft-deleted row is eligible regardless of age — a user's manual
        delete does not wait out the retention window.

        Caller MUST be inside an RLS bypass scope (the retention job's
        ``_bypass_rls``). The in-memory backend has no runs and no tenant
        config: it uses ``created_at`` / ``last_used_at`` and ``default_days``.
        """

    @abc.abstractmethod
    async def touch(self, *, image_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        """B-283 —— 记一次使用:``last_used_at = now``,只在这一行属于 ``tenant_id``、没删、
        且上次记的时间为空或早于 ``now - TOUCH_INTERVAL`` 时才写(每张每天最多一次)。
        ``True`` = 真的写了。"""

    @abc.abstractmethod
    async def hard_delete(self, *, image_ids: Sequence[UUID]) -> int:
        """Remove rows from the table; caller must have already cleared
        the corresponding object-store keys. Returns the count of rows
        actually deleted (idempotent — already-removed ids contribute 0).
        Any number of ids: the SQL store splits them into bounded ``IN``
        lists (B-280 — an agent purge passes every image of the agent)."""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete EVERY image upload for a user.

        Removes all of the user's ``image_upload`` rows (active AND soft-
        deleted) and returns the count deleted (0 when none / re-purge).
        Tenant- AND user-scoped — the ``(tenant_id, user_id)`` predicate never
        touches another tenant's or user's rows; rows with a NULL ``user_id``
        are not this user's and are left untouched. The object-store bytes are
        reaped separately by the retention sweep (orphaned key < stuck row)."""

    @abc.abstractmethod
    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int = 10000,
    ) -> list[ImageUpload]:
        """Deletion-hygiene purge (Task 8) — every row for a user, including
        soft-deleted ones.

        Unlike :meth:`list_active_for_thread`, this does NOT filter on
        ``deleted_at`` — ``purge_user`` needs the ``object_key`` of a
        soft-deleted row too (its blob hasn't necessarily been reaped yet).
        Tenant- AND user-scoped, same isolation rule as
        :meth:`delete_all_for_user`. Sorted by ``created_at`` ascending."""

    @abc.abstractmethod
    async def list_for_threads(
        self, *, tenant_id: UUID, thread_ids: Sequence[UUID]
    ) -> list[ImageUpload]:
        """B-278 —— every image of these conversations, active AND soft-deleted
        (the purge needs each ``object_key`` to delete the blob first)."""

    @abc.abstractmethod
    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """B-278 —— hard-delete the image rows of these conversations;
        tenant-scoped, returns the count deleted."""
