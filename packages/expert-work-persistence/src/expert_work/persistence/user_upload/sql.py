"""SQLAlchemy-backed ``UserUploadStore`` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Subquery, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.bulk_delete import count_in_chunks, delete_in_chunks
from expert_work.persistence.models import AgentRunRow, TenantConfigRow, UserUploadRow
from expert_work.persistence.user_upload.base import (
    TOUCH_INTERVAL,
    ExpiredUpload,
    UserUploadStore,
)
from expert_work.protocol import UserUpload, UserUploadKind


def _expiry(default_days: int, *candidates: ColumnElement[bool]) -> Subquery:
    """B-63 —— 候选行(没删、再加 ``candidates`` 条件)各自的到期时刻与「放弃」窗口的起点。

    * 到期 = ``greatest(created_at, last_used_at, 会话最后一次 run)`` + 租户保留期(没设用
      ``default_days``)。``greatest`` 跳过 NULL(没用过、会话没有 run)。
    * 会话最后一次 run 用 CTE **一次**算出:先取候选行涉及的 ``(tenant_id, thread_id)``,再对这些
      会话的 ``agent_run`` 按会话聚合一遍(走 ``ix_agent_run_thread_id``),然后 join 回来 ——
      不再每行一个相关子查询;同一会话里的多条上传共用一次聚合。只认**同租户**的 run。
    * ``give_up_from`` = ``greatest(到期, tenant_config.updated_at)``:租户刚把天数改小时,到期日
      一下子落到很久以前,「过了到期日 7 天还在失败就放弃」要从这次改动算起,不能第一次暂时失败就
      放弃。用配置行的 ``updated_at``(任何改动都会刷新它)是偏保守的近似:只会让放弃来得更晚。
    """
    cand = (
        select(
            UserUploadRow.id,
            UserUploadRow.tenant_id,
            UserUploadRow.thread_id,
            UserUploadRow.created_at,
            UserUploadRow.last_used_at,
        )
        .where(UserUploadRow.deleted_at.is_(None), *candidates)
        .cte("upload_candidates")
    )
    threads = select(cand.c.tenant_id, cand.c.thread_id).distinct().cte("upload_threads")
    last_run = (
        select(
            AgentRunRow.tenant_id,
            AgentRunRow.thread_id,
            func.max(AgentRunRow.created_at).label("at"),
        )
        .join(
            threads,
            and_(
                AgentRunRow.thread_id == threads.c.thread_id,
                AgentRunRow.tenant_id == threads.c.tenant_id,
            ),
        )
        .group_by(AgentRunRow.tenant_id, AgentRunRow.thread_id)
        .cte("upload_thread_last_run")
    )
    days = func.coalesce(TenantConfigRow.upload_retention_days, default_days)
    expires_at = func.greatest(
        cand.c.created_at, cand.c.last_used_at, last_run.c.at
    ) + func.make_interval(0, 0, 0, days)
    return (
        select(
            cand.c.id,
            expires_at.label("expires_at"),
            func.greatest(expires_at, TenantConfigRow.updated_at).label("give_up_from"),
        )
        .select_from(cand)
        .outerjoin(
            last_run,
            and_(
                last_run.c.thread_id == cand.c.thread_id,
                last_run.c.tenant_id == cand.c.tenant_id,
            ),
        )
        .outerjoin(TenantConfigRow, TenantConfigRow.tenant_id == cand.c.tenant_id)
        .subquery("upload_expiry")
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
