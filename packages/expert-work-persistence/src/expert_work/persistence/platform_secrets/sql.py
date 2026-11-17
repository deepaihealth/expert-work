"""SQLAlchemy-backed :class:`PlatformSecretStore` — Stream P (Mini-ADR P-7)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import (
    PlatformProviderSecretRow,
    PlatformToolSecretRow,
    TenantProviderSecretRow,
    TenantToolSecretRow,
)
from expert_work.persistence.platform_secrets.base import PlatformSecretStore
from expert_work.protocol import (
    PlatformProviderSecretRecord,
    PlatformToolSecretRecord,
    Provider,
    TenantProviderSecretRecord,
    TenantToolSecretRecord,
    Tool,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _provider_record(row: PlatformProviderSecretRow) -> PlatformProviderSecretRecord:
    return PlatformProviderSecretRecord(
        provider=cast(Provider, row.provider),
        key_id=row.key_id,
        secret_ref=row.secret_ref,
        enabled=row.enabled,
        priority=row.priority,
        base_url=row.base_url,
        base_url_unverified=row.base_url_unverified,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _tool_record(row: PlatformToolSecretRow) -> PlatformToolSecretRecord:
    return PlatformToolSecretRecord(
        tool=cast(Tool, row.tool),
        secret_ref=row.secret_ref,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _tenant_provider_record(row: TenantProviderSecretRow) -> TenantProviderSecretRecord:
    return TenantProviderSecretRecord(
        tenant_id=row.tenant_id,
        provider=cast(Provider, row.provider),
        secret_ref=row.secret_ref,
        enabled=row.enabled,
        base_url=row.base_url,
        base_url_unverified=row.base_url_unverified,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _tenant_tool_record(row: TenantToolSecretRow) -> TenantToolSecretRecord:
    return TenantToolSecretRecord(
        tenant_id=row.tenant_id,
        tool=cast(Tool, row.tool),
        secret_ref=row.secret_ref,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


class SqlPlatformSecretStore(PlatformSecretStore):
    """Postgres-backed platform secret-ref repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def list_providers(self) -> list[PlatformProviderSecretRecord]:
        async with self._sf() as session:
            rows = (await session.execute(select(PlatformProviderSecretRow))).scalars().all()
        return [_provider_record(r) for r in rows]

    async def get_provider(
        self, provider: Provider, key_id: str = "default"
    ) -> PlatformProviderSecretRecord | None:
        async with self._sf() as session:
            row = await session.get(PlatformProviderSecretRow, (provider, key_id))
        return _provider_record(row) if row is not None else None

    async def upsert_provider(
        self,
        *,
        provider: Provider,
        key_id: str = "default",
        secret_ref: str,
        enabled: bool,
        priority: int = 100,
        actor_id: str,
        base_url: str | None = None,
        base_url_unverified: bool = False,
    ) -> PlatformProviderSecretRecord:
        now = _utc_now()
        async with self._sf() as session:
            stmt = (
                pg_insert(PlatformProviderSecretRow)
                .values(
                    provider=provider,
                    key_id=key_id,
                    secret_ref=secret_ref,
                    enabled=enabled,
                    priority=priority,
                    base_url=base_url,
                    base_url_unverified=base_url_unverified,
                    created_at=now,
                    updated_at=now,
                    updated_by=actor_id,
                )
                .on_conflict_do_update(
                    index_elements=["provider", "key_id"],
                    set_={
                        "secret_ref": secret_ref,
                        "enabled": enabled,
                        "priority": priority,
                        "base_url": base_url,
                        "base_url_unverified": base_url_unverified,
                        "updated_at": now,
                        "updated_by": actor_id,
                    },
                )
                .returning(PlatformProviderSecretRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            await session.refresh(row)
            return _provider_record(row)

    async def delete_provider(self, provider: Provider, key_id: str = "default") -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(PlatformProviderSecretRow).where(
                    PlatformProviderSecretRow.provider == provider,
                    PlatformProviderSecretRow.key_id == key_id,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_tools(self) -> list[PlatformToolSecretRecord]:
        async with self._sf() as session:
            rows = (await session.execute(select(PlatformToolSecretRow))).scalars().all()
        return [_tool_record(r) for r in rows]

    async def get_tool(self, tool: Tool) -> PlatformToolSecretRecord | None:
        async with self._sf() as session:
            row = await session.get(PlatformToolSecretRow, tool)
        return _tool_record(row) if row is not None else None

    async def upsert_tool(
        self,
        *,
        tool: Tool,
        secret_ref: str,
        enabled: bool,
        actor_id: str,
    ) -> PlatformToolSecretRecord:
        now = _utc_now()
        async with self._sf() as session:
            stmt = (
                pg_insert(PlatformToolSecretRow)
                .values(
                    tool=tool,
                    secret_ref=secret_ref,
                    enabled=enabled,
                    created_at=now,
                    updated_at=now,
                    updated_by=actor_id,
                )
                .on_conflict_do_update(
                    index_elements=["tool"],
                    set_={
                        "secret_ref": secret_ref,
                        "enabled": enabled,
                        "updated_at": now,
                        "updated_by": actor_id,
                    },
                )
                .returning(PlatformToolSecretRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            await session.refresh(row)
            return _tool_record(row)

    async def delete_tool(self, tool: Tool) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(PlatformToolSecretRow).where(PlatformToolSecretRow.tool == tool)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    # --- per-tenant overrides (Stream HX-8) ---------------------------

    async def list_tenant_providers(
        self, tenant_id: UUID | None = None
    ) -> list[TenantProviderSecretRecord]:
        stmt = select(TenantProviderSecretRow)
        if tenant_id is not None:
            stmt = stmt.where(TenantProviderSecretRow.tenant_id == tenant_id)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_tenant_provider_record(r) for r in rows]

    async def upsert_tenant_provider(
        self,
        *,
        tenant_id: UUID,
        provider: Provider,
        secret_ref: str,
        enabled: bool,
        actor_id: str,
        base_url: str | None = None,
        base_url_unverified: bool = False,
    ) -> TenantProviderSecretRecord:
        now = _utc_now()
        async with self._sf() as session:
            stmt = (
                pg_insert(TenantProviderSecretRow)
                .values(
                    tenant_id=tenant_id,
                    provider=provider,
                    secret_ref=secret_ref,
                    enabled=enabled,
                    base_url=base_url,
                    base_url_unverified=base_url_unverified,
                    created_at=now,
                    updated_at=now,
                    updated_by=actor_id,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "provider"],
                    set_={
                        "secret_ref": secret_ref,
                        "enabled": enabled,
                        "base_url": base_url,
                        "base_url_unverified": base_url_unverified,
                        "updated_at": now,
                        "updated_by": actor_id,
                    },
                )
                .returning(TenantProviderSecretRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            await session.refresh(row)
            return _tenant_provider_record(row)

    async def delete_tenant_provider(self, *, tenant_id: UUID, provider: Provider) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(TenantProviderSecretRow).where(
                    TenantProviderSecretRow.tenant_id == tenant_id,
                    TenantProviderSecretRow.provider == provider,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_tenant_tools(
        self, tenant_id: UUID | None = None
    ) -> list[TenantToolSecretRecord]:
        stmt = select(TenantToolSecretRow)
        if tenant_id is not None:
            stmt = stmt.where(TenantToolSecretRow.tenant_id == tenant_id)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_tenant_tool_record(r) for r in rows]

    async def upsert_tenant_tool(
        self,
        *,
        tenant_id: UUID,
        tool: Tool,
        secret_ref: str,
        enabled: bool,
        actor_id: str,
    ) -> TenantToolSecretRecord:
        now = _utc_now()
        async with self._sf() as session:
            stmt = (
                pg_insert(TenantToolSecretRow)
                .values(
                    tenant_id=tenant_id,
                    tool=tool,
                    secret_ref=secret_ref,
                    enabled=enabled,
                    created_at=now,
                    updated_at=now,
                    updated_by=actor_id,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "tool"],
                    set_={
                        "secret_ref": secret_ref,
                        "enabled": enabled,
                        "updated_at": now,
                        "updated_by": actor_id,
                    },
                )
                .returning(TenantToolSecretRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            await session.refresh(row)
            return _tenant_tool_record(row)

    async def delete_tenant_tool(self, *, tenant_id: UUID, tool: Tool) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(TenantToolSecretRow).where(
                    TenantToolSecretRow.tenant_id == tenant_id,
                    TenantToolSecretRow.tool == tool,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0
