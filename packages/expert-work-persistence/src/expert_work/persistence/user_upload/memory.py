"""In-memory ``UserUploadStore`` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.user_upload.base import UserUploadStore
from expert_work.protocol import UserUpload, UserUploadKind


class InMemoryUserUploadStore(UserUploadStore):
    """Single-process registry — used by tests + dev default.

    Keyed by ``(tenant_id, id)`` — mirrors the SQL store's
    ``WHERE id = :id AND tenant_id = :tenant`` predicate exactly (a
    plain ``dict[id, row]`` would let a colliding id from another tenant
    shadow this one, which the SQL store can never do).
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID], UserUpload] = {}

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
        row = UserUpload(
            id=upload_id,
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            kind=kind,
            ref=ref,
            mime_type=mime_type,
            size_bytes=size_bytes,
            filename=filename,
            created_at=datetime.now(UTC),
            deleted_at=None,
        )
        self._rows[(tenant_id, upload_id)] = row
        return row

    async def get(self, *, upload_id: UUID, tenant_id: UUID) -> UserUpload | None:
        return self._rows.get((tenant_id, upload_id))

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [
            key
            for key, r in self._rows.items()
            if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for key in victims:
            del self._rows[key]
        return len(victims)
