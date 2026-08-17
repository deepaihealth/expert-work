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
    ) -> UserUpload:
        """Persist one row for a successful upload.

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
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete EVERY upload row for a user.

        Tenant- AND user-scoped — the ``(tenant_id, user_id)`` predicate
        never touches another tenant's or user's rows. Returns the count
        deleted (0 when none / re-purge)."""
