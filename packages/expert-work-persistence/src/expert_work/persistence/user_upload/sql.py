"""SQLAlchemy-backed ``UserUploadStore`` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Subquery, and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.bulk_delete import CHUNK, count_in_chunks, delete_in_chunks
from expert_work.persistence.models import TenantConfigRow, UserUploadRow
from expert_work.persistence.retention_clock import last_use_expiry
from expert_work.persistence.user_upload.base import (
    GIVE_UP_DEFER_CAP,
    TOUCH_INTERVAL,
    ExpiredUpload,
    UserUploadStore,
)
from expert_work.protocol import UserUpload, UserUploadKind
from expert_work.protocol.user_upload import SESSION_DELETED_TOMBSTONE_REF


def _expiry(default_days: int, *candidates: ColumnElement[bool]) -> Subquery:
    """B-63 —— 候选行(没删、再加 ``candidates`` 条件)各自的到期时刻与「放弃」窗口的起点;
    时钟与图片共用 :func:`expert_work.persistence.retention_clock.last_use_expiry`。"""
    return last_use_expiry(
        UserUploadRow,
        days_column=TenantConfigRow.upload_retention_days,
        default_days=default_days,
        give_up_cap=GIVE_UP_DEFER_CAP,
        name="upload",
        candidates=candidates,
    )


def _row_to_dto(row: UserUploadRow) -> UserUpload:
    return UserUpload(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        thread_id=row.thread_id,
        kind=row.kind,  # type: ignore[arg-type]
        ref=row.ref,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        filename=row.filename,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
        sha256=row.sha256,
        last_used_at=row.last_used_at,
    )


class SqlUserUploadStore(UserUploadStore):
    """Postgres-backed third-party attachment registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

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
        now = datetime.now(UTC)
        async with self._sf() as session:
            row = UserUploadRow(
                id=upload_id,
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=thread_id,
                kind=kind,
                ref=ref,
                mime_type=mime_type,
                size_bytes=size_bytes,
                filename=filename,
                created_at=now,
                deleted_at=None,
                sha256=sha256,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _row_to_dto(row)

    async def get(self, *, upload_id: UUID, tenant_id: UUID) -> UserUpload | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(UserUploadRow).where(
                        UserUploadRow.id == upload_id,
                        UserUploadRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def list_documents_for_thread(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[UserUpload]:
        stmt = (
            select(UserUploadRow)
            .where(
                UserUploadRow.tenant_id == tenant_id,
                UserUploadRow.thread_id == thread_id,
                UserUploadRow.kind == "document",
                UserUploadRow.deleted_at.is_(None),
            )
            .order_by(UserUploadRow.created_at.asc(), UserUploadRow.id.asc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(row) for row in rows]

    async def list_expired(
        self, *, now: datetime, default_days: int, limit: int = 1000
    ) -> list[ExpiredUpload]:
        expiry = _expiry(default_days)
        stmt = (
            select(UserUploadRow, expiry.c.expires_at, expiry.c.give_up_from)
            .join(expiry, expiry.c.id == UserUploadRow.id)
            .where(expiry.c.expires_at < now)
            .order_by(expiry.c.expires_at.asc(), UserUploadRow.id.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [
            ExpiredUpload(upload=_row_to_dto(row), expires_at=at, give_up_from=give_up)
            for row, at, give_up in rows
        ]

    async def list_unexpired_by_ref(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        ref: str,
        now: datetime,
        default_days: int,
    ) -> list[UserUpload]:
        expiry = _expiry(
            default_days,
            UserUploadRow.tenant_id == tenant_id,
            UserUploadRow.user_id == user_id,
            UserUploadRow.ref == ref,
        )
        stmt = (
            select(UserUploadRow)
            .join(expiry, expiry.c.id == UserUploadRow.id)
            .where(expiry.c.expires_at >= now)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(row) for row in rows]

    async def touch(self, *, upload_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(UserUploadRow)
                .where(
                    UserUploadRow.id == upload_id,
                    UserUploadRow.tenant_id == tenant_id,
                    UserUploadRow.deleted_at.is_(None),
                    or_(
                        UserUploadRow.last_used_at.is_(None),
                        UserUploadRow.last_used_at < now - TOUCH_INTERVAL,
                    ),
                )
                .values(last_used_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def soft_delete(self, *, upload_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(UserUploadRow)
                .where(
                    UserUploadRow.id == upload_id,
                    UserUploadRow.tenant_id == tenant_id,
                    UserUploadRow.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def hard_delete(self, *, upload_id: UUID, tenant_id: UUID) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(UserUploadRow).where(
                    UserUploadRow.id == upload_id, UserUploadRow.tenant_id == tenant_id
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        async with self._sf() as session:
            result = await session.execute(
                delete(UserUploadRow).where(
                    UserUploadRow.tenant_id == tenant_id,
                    UserUploadRow.user_id == user_id,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def count_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        return await count_in_chunks(
            self._sf,
            UserUploadRow,
            UserUploadRow.thread_id,
            list(thread_ids),
            UserUploadRow.tenant_id == tenant_id,
        )

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        return await delete_in_chunks(
            self._sf,
            UserUploadRow,
            UserUploadRow.thread_id,
            list(thread_ids),
            UserUploadRow.tenant_id == tenant_id,
        )

    async def tombstone_for_threads(
        self, *, tenant_id: UUID, thread_ids: Sequence[UUID], now: datetime
    ) -> int:
        ids = list(thread_ids)
        handled = 0
        for i in range(0, len(ids), CHUNK):
            scope = (
                UserUploadRow.tenant_id == tenant_id,
                UserUploadRow.thread_id.in_(ids[i : i + CHUNK]),
            )
            async with self._sf() as session:
                # Images and already-expired rows go; existing tombstones (empty ``ref``) stay.
                gone = await session.execute(
                    delete(UserUploadRow).where(
                        *scope,
                        or_(
                            UserUploadRow.kind == "image",
                            and_(
                                UserUploadRow.deleted_at.is_not(None),
                                UserUploadRow.ref != SESSION_DELETED_TOMBSTONE_REF,
                            ),
                        ),
                    )
                )
                scrubbed = await session.execute(
                    update(UserUploadRow)
                    .where(
                        *scope,
                        UserUploadRow.kind == "document",
                        UserUploadRow.deleted_at.is_(None),
                    )
                    .values(
                        ref=SESSION_DELETED_TOMBSTONE_REF,
                        filename="",
                        mime_type="",
                        size_bytes=0,
                        sha256=None,
                        last_used_at=None,
                        deleted_at=now,
                    )
                )
                await session.commit()
            handled += int(getattr(gone, "rowcount", 0) or 0)
            handled += int(getattr(scrubbed, "rowcount", 0) or 0)
        return handled
