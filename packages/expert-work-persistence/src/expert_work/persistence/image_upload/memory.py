"""In-memory ``ImageUploadStore`` — Stream J.6.补强-3 (Mini-ADR J-32)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.image_upload.base import ImageUploadStore
from expert_work.protocol import ImageUpload


class InMemoryImageUploadStore(ImageUploadStore):
    """Single-process registry — used by tests + dev default."""

    def __init__(self) -> None:
        self._rows: dict[UUID, ImageUpload] = {}

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
        row = ImageUpload(
            id=image_id,
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_id=user_id,
            object_key=object_key,
            size_bytes=size_bytes,
            mime_type=mime_type,
            sha256=sha256,
            created_at=datetime.now(UTC),
            deleted_at=None,
        )
        self._rows[image_id] = row
        return row

    async def get(self, *, image_id: UUID, tenant_id: UUID) -> ImageUpload | None:
        row = self._rows.get(image_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def soft_delete(self, *, image_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        row = self._rows.get(image_id)
        if row is None or row.tenant_id != tenant_id or row.deleted_at is not None:
            return False
        self._rows[image_id] = row.model_copy(update={"deleted_at": now})
        return True

    async def list_active_for_thread(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
    ) -> list[ImageUpload]:
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id and r.thread_id == thread_id and r.deleted_at is None
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    async def list_reapable(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[ImageUpload]:
        rows = [r for r in self._rows.values() if r.created_at < before or r.deleted_at is not None]
        rows.sort(key=lambda r: r.created_at)
        return rows[:limit]

    async def hard_delete(self, *, image_ids: Sequence[UUID]) -> int:
        removed = 0
        for image_id in image_ids:
            if image_id in self._rows:
                del self._rows[image_id]
                removed += 1
        return removed

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [
            image_id
            for image_id, r in self._rows.items()
            if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for image_id in victims:
            del self._rows[image_id]
        return len(victims)

    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int = 10000,
    ) -> list[ImageUpload]:
        rows = [r for r in self._rows.values() if r.tenant_id == tenant_id and r.user_id == user_id]
        rows.sort(key=lambda r: r.created_at)
        return rows[:limit]

    async def list_for_threads(
        self, *, tenant_id: UUID, thread_ids: Sequence[UUID]
    ) -> list[ImageUpload]:
        wanted = set(thread_ids)
        rows = [
            r for r in self._rows.values() if r.tenant_id == tenant_id and r.thread_id in wanted
        ]
        rows.sort(key=lambda r: r.created_at)
        return rows

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        wanted = set(thread_ids)
        victims = [
            image_id
            for image_id, r in self._rows.items()
            if r.tenant_id == tenant_id and r.thread_id in wanted
        ]
        for image_id in victims:
            del self._rows[image_id]
        return len(victims)
