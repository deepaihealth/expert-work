"""SQLAlchemy-backed ``UserUploadStore`` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import UserUploadRow
from expert_work.persistence.user_upload.base import UserUploadStore
from expert_work.protocol import UserUpload, UserUploadKind


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
