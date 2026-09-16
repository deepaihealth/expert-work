"""SQLAlchemy-backed :class:`PlatformAgentTemplateStore` (Postgres / asyncpg)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import PlatformAgentTemplateRow
from expert_work.persistence.platform_agent_template.base import (
    PlatformAgentTemplateAlreadyExistsError,
    PlatformAgentTemplateNotFoundError,
    PlatformAgentTemplateStore,
    compute_spec_sha256,
)
from expert_work.persistence.stored_spec import load_stored_spec
from expert_work.protocol import (
    PlatformAgentTemplatePatch,
    PlatformAgentTemplateRecord,
    PlatformAgentTemplateStatus,
    PlatformAgentTemplateUpsert,
)
from expert_work.protocol.agent_spec import AgentSpec
from expert_work.protocol.tenant_config import TenantPlan


def _row_to_record(row: PlatformAgentTemplateRow) -> PlatformAgentTemplateRecord:
    return PlatformAgentTemplateRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        version=row.version,
        spec=load_stored_spec(row.spec_json),
        spec_sha256=row.spec_sha256,
        display_name=row.display_name,
        description=row.description,
        category=row.category,
        icon=row.icon,
        required_tier=TenantPlan(row.required_tier),
        status=PlatformAgentTemplateStatus(row.status),
        enabled=row.enabled,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlPlatformAgentTemplateStore(PlatformAgentTemplateStore):
    """Postgres-backed Agent template catalog. Every write sets ``tenant_id=None``
    (platform-global). Callers MUST be inside ``bypass_rls_session()``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self, *, upsert: PlatformAgentTemplateUpsert, created_by: str
    ) -> PlatformAgentTemplateRecord:
        now = datetime.now(UTC)
        name = upsert.spec.metadata.name
        version = upsert.spec.metadata.version
        row = PlatformAgentTemplateRow(
            tenant_id=None,
            name=name,
            version=version,
            spec_json=upsert.spec.model_dump(by_alias=True, mode="json"),
            spec_sha256=compute_spec_sha256(upsert.spec),
            display_name=upsert.display_name,
            description=upsert.description,
            category=upsert.category,
            icon=upsert.icon,
            required_tier=upsert.required_tier.value,
            status=upsert.status.value,
            enabled=upsert.enabled,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise PlatformAgentTemplateAlreadyExistsError(name=name, version=version) from exc
            await session.refresh(row)
            return _row_to_record(row)

    async def get(self, *, name: str, version: str) -> PlatformAgentTemplateRecord | None:
        stmt = select(PlatformAgentTemplateRow).where(
            PlatformAgentTemplateRow.name == name,
            PlatformAgentTemplateRow.version == version,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_record(row) if row is not None else None

    async def get_latest(
        self, *, name: str, status: PlatformAgentTemplateStatus | None = None
    ) -> PlatformAgentTemplateRecord | None:
        stmt = select(PlatformAgentTemplateRow).where(PlatformAgentTemplateRow.name == name)
        if status is not None:
            stmt = stmt.where(PlatformAgentTemplateRow.status == status.value)
        stmt = stmt.order_by(PlatformAgentTemplateRow.created_at.desc()).limit(1)
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_record(row) if row is not None else None

    async def list_versions(self, *, name: str) -> list[PlatformAgentTemplateRecord]:
        stmt = (
            select(PlatformAgentTemplateRow)
            .where(PlatformAgentTemplateRow.name == name)
            .order_by(PlatformAgentTemplateRow.created_at.desc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def list(
        self,
        *,
        category: str | None = None,
        status: PlatformAgentTemplateStatus | None = None,
    ) -> list[PlatformAgentTemplateRecord]:
        stmt = select(PlatformAgentTemplateRow)
        if category is not None:
            stmt = stmt.where(PlatformAgentTemplateRow.category == category)
        if status is not None:
            stmt = stmt.where(PlatformAgentTemplateRow.status == status.value)
        stmt = stmt.order_by(
            PlatformAgentTemplateRow.name.asc(),
            PlatformAgentTemplateRow.created_at.desc(),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def update_spec(
        self,
        *,
        name: str,
        version: str,
        spec: AgentSpec,
        updated_by: str,
    ) -> PlatformAgentTemplateRecord | None:
        now = datetime.now(UTC)
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(PlatformAgentTemplateRow)
                    .where(
                        PlatformAgentTemplateRow.name == name,
                        PlatformAgentTemplateRow.version == version,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.spec_json = spec.model_dump(by_alias=True, mode="json")
            row.spec_sha256 = compute_spec_sha256(spec)
            row.created_by = updated_by
            row.updated_at = now
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row)

    async def update_meta(
        self, *, name: str, version: str, patch: PlatformAgentTemplatePatch
    ) -> PlatformAgentTemplateRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(PlatformAgentTemplateRow)
                    .where(
                        PlatformAgentTemplateRow.name == name,
                        PlatformAgentTemplateRow.version == version,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if patch.display_name is not None:
                row.display_name = patch.display_name
            if patch.description is not None:
                row.description = patch.description
            if patch.category is not None:
                row.category = patch.category
            if patch.icon is not None:
                row.icon = patch.icon
            if patch.required_tier is not None:
                row.required_tier = patch.required_tier.value
            if patch.status is not None:
                row.status = patch.status.value
            if patch.enabled is not None:
                row.enabled = patch.enabled
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row)

    async def delete(self, *, name: str, version: str) -> None:
        stmt = (
            sa_delete(PlatformAgentTemplateRow)
            .where(
                PlatformAgentTemplateRow.name == name,
                PlatformAgentTemplateRow.version == version,
            )
            .returning(PlatformAgentTemplateRow.id)
        )
        async with self._sf() as session:
            deleted = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
        if deleted is None:
            raise PlatformAgentTemplateNotFoundError(name=name, version=version)
