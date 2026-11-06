"""SQLAlchemy-backed ``ImageUploadStore`` — Stream J.6.补强-3 (Mini-ADR J-32)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.bulk_delete import CHUNK, delete_in_chunks
from expert_work.persistence.image_upload.base import ImageUploadStore
from expert_work.persistence.models import ImageUploadRow, TenantConfigRow
from expert_work.persistence.retention_clock import last_use_expiry
from expert_work.persistence.user_upload.base import GIVE_UP_DEFER_CAP, TOUCH_INTERVAL
from expert_work.protocol import ImageUpload


def _expired_ids(now: datetime, default_days: int) -> Select[tuple[UUID]]:
    """B-283 —— 没删、按**最后使用**已经到期的图片 id。时钟与附件(``user_upload``)是同一个
    (:func:`expert_work.persistence.retention_clock.last_use_expiry`),天数也是同一个字段
    ``tenant_config.upload_retention_days``:图片也是附件,登记行与字节不会在不同的日子到期。"""
    expiry = last_use_expiry(
        ImageUploadRow,
        days_column=TenantConfigRow.upload_retention_days,
        default_days=default_days,
        give_up_cap=GIVE_UP_DEFER_CAP,
        name="image",
    )
    return select(expiry.c.id).where(expiry.c.expires_at < now)


def _row_to_dto(row: ImageUploadRow) -> ImageUpload:
    return ImageUpload(
        id=row.id,
        tenant_id=row.tenant_id,
        thread_id=row.thread_id,
        user_id=row.user_id,
        object_key=row.object_key,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type,
        sha256=row.sha256,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
        last_used_at=row.last_used_at,
    )


class SqlImageUploadStore(ImageUploadStore):
    """Postgres-backed image upload registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

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
        now = datetime.now(UTC)
        async with self._sf() as session:
            row = ImageUploadRow(
                id=image_id,
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=thread_id,
                object_key=object_key,
                size_bytes=size_bytes,
                mime_type=mime_type,
                sha256=sha256,
                created_at=now,
                deleted_at=None,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _row_to_dto(row)

    async def get(self, *, image_id: UUID, tenant_id: UUID) -> ImageUpload | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(ImageUploadRow).where(
                        ImageUploadRow.id == image_id,
                        ImageUploadRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def soft_delete(self, *, image_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(ImageUploadRow)
                .where(
                    ImageUploadRow.id == image_id,
                    ImageUploadRow.tenant_id == tenant_id,
                    ImageUploadRow.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_active_for_thread(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
    ) -> list[ImageUpload]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(ImageUploadRow)
                        .where(
                            ImageUploadRow.tenant_id == tenant_id,
                            ImageUploadRow.thread_id == thread_id,
                            ImageUploadRow.deleted_at.is_(None),
                        )
                        .order_by(ImageUploadRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def list_reapable(
        self,
        *,
        now: datetime,
        default_days: int,
        limit: int = 1000,
    ) -> list[ImageUpload]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(ImageUploadRow)
                        .where(
                            or_(
                                ImageUploadRow.deleted_at.is_not(None),
                                ImageUploadRow.id.in_(_expired_ids(now, default_days)),
                            )
                        )
                        .order_by(ImageUploadRow.created_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def touch(self, *, image_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(ImageUploadRow)
                .where(
                    ImageUploadRow.id == image_id,
                    ImageUploadRow.tenant_id == tenant_id,
                    ImageUploadRow.deleted_at.is_(None),
                    or_(
                        ImageUploadRow.last_used_at.is_(None),
                        ImageUploadRow.last_used_at < now - TOUCH_INTERVAL,
                    ),
                )
                .values(last_used_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def hard_delete(self, *, image_ids: Sequence[UUID]) -> int:
        # ``IN`` lists in chunks: asyncpg caps one statement at 32,767 bind
        # parameters, and an agent purge passes every image of the agent.
        ids = list(image_ids)
        removed = 0
        for i in range(0, len(ids), CHUNK):
            async with self._sf() as session:
                result = await session.execute(
                    delete(ImageUploadRow).where(ImageUploadRow.id.in_(ids[i : i + CHUNK]))
                )
                await session.commit()
            removed += int(getattr(result, "rowcount", 0) or 0)
        return removed

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        async with self._sf() as session:
            result = await session.execute(
                delete(ImageUploadRow).where(
                    ImageUploadRow.tenant_id == tenant_id,
                    ImageUploadRow.user_id == user_id,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int = 10000,
    ) -> list[ImageUpload]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(ImageUploadRow)
                        .where(
                            ImageUploadRow.tenant_id == tenant_id,
                            ImageUploadRow.user_id == user_id,
                        )
                        .order_by(ImageUploadRow.created_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def list_for_threads(
        self, *, tenant_id: UUID, thread_ids: Sequence[UUID]
    ) -> list[ImageUpload]:
        ids = list(thread_ids)
        rows: list[ImageUploadRow] = []
        async with self._sf() as session:
            for i in range(0, len(ids), CHUNK):
                rows.extend(
                    (
                        await session.execute(
                            select(ImageUploadRow).where(
                                ImageUploadRow.tenant_id == tenant_id,
                                ImageUploadRow.thread_id.in_(ids[i : i + CHUNK]),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        rows.sort(key=lambda r: r.created_at)
        return [_row_to_dto(r) for r in rows]

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        return await delete_in_chunks(
            self._sf,
            ImageUploadRow,
            ImageUploadRow.thread_id,
            list(thread_ids),
            ImageUploadRow.tenant_id == tenant_id,
        )
