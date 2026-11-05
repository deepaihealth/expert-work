"""In-memory ``UserUploadStore`` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from expert_work.persistence.user_upload.base import (
    TOUCH_INTERVAL,
    ExpiredUpload,
    UserUploadStore,
)
from expert_work.protocol import UserUpload, UserUploadKind


def _expires_at(row: UserUpload, default_days: int) -> datetime:
    """B-63 —— 与 SQL 实现同一个时钟,但内存里没有 run 与租户配置:只看 ``created_at`` /
    ``last_used_at`` 与 ``default_days``(会话最后一次 run、租户保留期由 SQL 集成测试覆盖)。"""
    last_active = max(t for t in (row.created_at, row.last_used_at) if t is not None)
    return last_active + timedelta(days=default_days)


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
        sha256: str | None = None,
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
            sha256=sha256,
        )
        self._rows[(tenant_id, upload_id)] = row
        return row

    async def get(self, *, upload_id: UUID, tenant_id: UUID) -> UserUpload | None:
        return self._rows.get((tenant_id, upload_id))

    async def list_documents_for_thread(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[UserUpload]:
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and r.thread_id == thread_id
            and r.kind == "document"
            and r.deleted_at is None
        ]
        rows.sort(key=lambda r: (r.created_at, str(r.id)))
        return rows

    async def list_expired(
        self, *, now: datetime, default_days: int, limit: int = 1000
    ) -> list[ExpiredUpload]:
        due = [
            ExpiredUpload(upload=r, expires_at=_expires_at(r, default_days))
            for r in self._rows.values()
            if r.deleted_at is None
        ]
        expired = [e for e in due if e.expires_at < now]
        expired.sort(key=lambda e: (e.expires_at, str(e.upload.id)))
        return expired[:limit]

    async def list_unexpired_by_ref(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        ref: str,
        now: datetime,
        default_days: int,
    ) -> list[UserUpload]:
        return [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and r.user_id == user_id
            and r.ref == ref
            and r.deleted_at is None
            and _expires_at(r, default_days) >= now
        ]

    async def touch(self, *, upload_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        row = self._rows.get((tenant_id, upload_id))
        if row is None or row.deleted_at is not None:
            return False
        if row.last_used_at is not None and row.last_used_at >= now - TOUCH_INTERVAL:
            return False
        self._rows[(tenant_id, upload_id)] = row.model_copy(update={"last_used_at": now})
        return True

    async def soft_delete(self, *, upload_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        row = self._rows.get((tenant_id, upload_id))
        if row is None or row.deleted_at is not None:
            return False
        self._rows[(tenant_id, upload_id)] = row.model_copy(update={"deleted_at": now})
        return True

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [
            key
            for key, r in self._rows.items()
            if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for key in victims:
            del self._rows[key]
        return len(victims)

    def _thread_keys(self, tenant_id: UUID, thread_ids: Sequence[UUID]) -> list[tuple[UUID, UUID]]:
        wanted = set(thread_ids)
        return [
            key
            for key, r in self._rows.items()
            if r.tenant_id == tenant_id and r.thread_id in wanted
        ]

    async def count_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        return len(self._thread_keys(tenant_id, thread_ids))

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        victims = self._thread_keys(tenant_id, thread_ids)
        for key in victims:
            del self._rows[key]
        return len(victims)
