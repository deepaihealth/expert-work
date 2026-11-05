"""SQLAlchemy-backed ``UserUploadStore`` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.bulk_delete import count_in_chunks, delete_in_chunks
from expert_work.persistence.models import AgentRunRow, TenantConfigRow, UserUploadRow
from expert_work.persistence.user_upload.base import (
    TOUCH_INTERVAL,
    ExpiredUpload,
    UserUploadStore,
)
from expert_work.protocol import UserUpload, UserUploadKind


def _expires_at(default_days: int) -> ColumnElement[datetime]:
    """B-63 —— 一行上传的到期时刻:``greatest(created_at, last_used_at, 会话最后一次 run)``
    加上租户的保留期(没设用 ``default_days``)。

    ``greatest`` 跳过 NULL(没用过、会话没有 run)。会话最后一次 run 走 ``ix_agent_run_thread_id``,
    与 ``thread_meta`` 列表按最后活动排序同一个口径;租户的保留期是 ``tenant_config`` 主键查找。
    """
    last_run = (
        select(func.max(AgentRunRow.created_at))
        .where(
            AgentRunRow.thread_id == UserUploadRow.thread_id,
            AgentRunRow.tenant_id == UserUploadRow.tenant_id,
        )
        .scalar_subquery()
    )
    tenant_days = (
        select(TenantConfigRow.upload_retention_days)
        .where(TenantConfigRow.tenant_id == UserUploadRow.tenant_id)
        .scalar_subquery()
    )
    last_active = func.greatest(UserUploadRow.created_at, UserUploadRow.last_used_at, last_run)
    days = func.coalesce(tenant_days, default_days)
    return last_active + func.make_interval(0, 0, 0, days)


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
        expires_at = _expires_at(default_days).label("expires_at")
        inner = (
            select(UserUploadRow.id, expires_at)
            .where(UserUploadRow.deleted_at.is_(None))
            .subquery()
        )
        stmt = (
            select(UserUploadRow, inner.c.expires_at)
            .join(inner, inner.c.id == UserUploadRow.id)
            .where(inner.c.expires_at < now)
            .order_by(inner.c.expires_at.asc(), UserUploadRow.id.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [ExpiredUpload(upload=_row_to_dto(row), expires_at=at) for row, at in rows]

    async def list_unexpired_by_ref(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        ref: str,
        now: datetime,
        default_days: int,
    ) -> list[UserUpload]:
        stmt = select(UserUploadRow).where(
            UserUploadRow.tenant_id == tenant_id,
            UserUploadRow.user_id == user_id,
            UserUploadRow.ref == ref,
            UserUploadRow.deleted_at.is_(None),
            _expires_at(default_days) >= now,
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
