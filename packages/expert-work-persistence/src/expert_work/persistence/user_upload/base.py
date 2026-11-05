"""Abstract ``UserUploadStore`` repository —— 附件模型统一(spec 2026-08-17)。

Implementations:

* :class:`expert_work.persistence.user_upload.memory.InMemoryUserUploadStore`
* :class:`expert_work.persistence.user_upload.sql.SqlUserUploadStore`

The store is scoped by ``tenant_id`` at the application layer; the SQL
implementation also applies a tenant RLS policy (migration 0146) so a
forgotten WHERE clause cannot cross-leak.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from expert_work.protocol import UserUpload, UserUploadKind


class UserUploadStore(abc.ABC):
    """Per-(tenant, user) registry of landed third-party attachments.

    ``get`` intentionally filters only on ``(id, tenant_id)`` — it does
    **not** filter on ``user_id`` or ``deleted_at`` — the caller compares
    those itself (same rule as :class:`ImageUploadStore.get`).
    """

    @abc.abstractmethod
    async def insert(
        self,
        *,
        upload_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        thread_id: UUID,
        kind: UserUploadKind,
        ref: str,
        mime_type: str,
        size_bytes: int,
        filename: str,
        sha256: str | None = None,
    ) -> UserUpload:
        """Persist one row for a successful upload.

        ``sha256`` (B-271) is the hex digest of the landed bytes; the retention
        sweep compares it before deleting the file.

        Returns the materialised row including the server-assigned
        ``created_at`` so the caller can echo it back in the API
        response without a re-read.
        """

    @abc.abstractmethod
    async def get(self, *, upload_id: UUID, tenant_id: UUID) -> UserUpload | None:
        """Return the row by id, or ``None`` when the id is unknown or
        belongs to a different tenant. Does **not** filter on ``user_id``
        or ``deleted_at`` — the caller compares both itself."""

    @abc.abstractmethod
    async def list_documents_for_thread(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[UserUpload]:
        """B-262 P3 —— 这个会话自己的文档类上传(没删的),按上传先后。老对话迁移按它找本对话
        的上传件。"""

    @abc.abstractmethod
    async def list_expired(self, *, before: datetime, limit: int = 1000) -> list[UserUpload]:
        """留存链(上传 90 天)—— 跨租户列出 ``deleted_at IS NULL AND created_at <
        before`` 的行,最老在前。Caller MUST be inside an RLS bypass scope(the
        retention job's ``_bypass_rls``)。Already soft-deleted rows are not
        candidates — the sweep marks, it does not re-mark."""

    @abc.abstractmethod
    async def list_active_by_ref(
        self, *, tenant_id: UUID, user_id: UUID, ref: str
    ) -> list[UserUpload]:
        """B-271 —— 同租户、同用户、同 ``ref``、没删(``deleted_at IS NULL``)的行,不论会话、
        不论是否已到期。留存作业的引用检查用它找「还有谁可能指着同一个文件」(老视图同一 agent
        的各对话共用 ``uploads/``,同名同内容复用让多行指向一个文件)。"""

    @abc.abstractmethod
    async def soft_delete(self, *, upload_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        """Flip ``deleted_at`` on an active row; ``True`` on that transition.

        ``False`` when the id is unknown for this tenant or already
        soft-deleted (idempotent). The external read surfaces already treat
        ``deleted_at IS NOT NULL`` as 404 (``external_uploads.py``), so this is
        the whole "expired" state — no new column."""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete EVERY upload row for a user.

        Tenant- AND user-scoped — the ``(tenant_id, user_id)`` predicate
        never touches another tenant's or user's rows. Returns the count
        deleted (0 when none / re-purge)."""

    @abc.abstractmethod
    async def count_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """B-278 —— rows ``delete_for_threads`` would remove (agent purge preview)."""

    @abc.abstractmethod
    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """B-278 —— hard-delete the upload registry rows of these conversations;
        tenant-scoped, returns the count deleted."""
