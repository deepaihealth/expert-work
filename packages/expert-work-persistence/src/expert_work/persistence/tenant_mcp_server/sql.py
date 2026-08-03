"""Postgres-backed :class:`TenantMcpServerStore` — Stream V."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import TenantMcpServerRow
from expert_work.persistence.tenant_mcp_server.base import (
    TenantMcpServerAlreadyExistsError,
    TenantMcpServerNotFoundError,
    TenantMcpServerStore,
)
from expert_work.protocol import (
    McpServerAuthType,
    McpServerProbeStatus,
    McpServerTransport,
    TenantMcpServerPatch,
    TenantMcpServerRecord,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _row_to_record(row: TenantMcpServerRow) -> TenantMcpServerRecord:
    return TenantMcpServerRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        transport=row.transport,  # type: ignore[arg-type]
        url=row.url,
        auth_type=row.auth_type,  # type: ignore[arg-type]
        token_secret_ref=row.token_secret_ref,
        custom_headers_ref=row.custom_headers_ref,
        custom_header_names=row.custom_header_names,
        sse_read_timeout_s=row.sse_read_timeout_s,
        catalog_id=row.catalog_id,
        timeout_s=row.timeout_s,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        last_probe_at=row.last_probe_at,
        last_probe_status=row.last_probe_status,  # type: ignore[arg-type]
        last_probe_error=row.last_probe_error,
    )


class SqlTenantMcpServerStore(TenantMcpServerStore):
    """Postgres-backed tenant MCP server registry (RLS-scoped sessions)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self,
        *,
        tenant_id: UUID,
        name: str,
        transport: McpServerTransport,
        url: str,
        auth_type: McpServerAuthType,
        token_secret_ref: str | None,
        timeout_s: float,
        created_by: str,
        catalog_id: UUID | None = None,
        custom_headers_ref: str | None = None,
        custom_header_names: list[str] | None = None,
        sse_read_timeout_s: float | None = None,
    ) -> TenantMcpServerRecord:
        now = _utc_now()
        stmt = (
            pg_insert(TenantMcpServerRow)
            .values(
                tenant_id=tenant_id,
                name=name,
                transport=transport,
                url=url,
                auth_type=auth_type,
                token_secret_ref=token_secret_ref,
                custom_headers_ref=custom_headers_ref,
                custom_header_names=custom_header_names,
                sse_read_timeout_s=sse_read_timeout_s,
                catalog_id=catalog_id,
                timeout_s=timeout_s,
                enabled=True,
                created_at=now,
                updated_at=now,
                created_by=created_by,
            )
            .returning(TenantMcpServerRow)
        )
        async with self._sf() as session:
            try:
                row = (await session.execute(stmt)).scalar_one()
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise TenantMcpServerAlreadyExistsError(tenant_id=tenant_id, name=name) from exc
            await session.refresh(row)
            return _row_to_record(row)

    async def get(self, *, tenant_id: UUID, name: str) -> TenantMcpServerRecord | None:
        stmt = select(TenantMcpServerRow).where(
            TenantMcpServerRow.tenant_id == tenant_id,
            TenantMcpServerRow.name == name,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_record(row) if row is not None else None

    async def list_for_tenant(self, *, tenant_id: UUID) -> list[TenantMcpServerRecord]:
        stmt = (
            select(TenantMcpServerRow)
            .where(TenantMcpServerRow.tenant_id == tenant_id)
            .order_by(TenantMcpServerRow.name)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def list_all_tenants(self) -> list[TenantMcpServerRecord]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        # tenant_id tiebreak: (tenant_id, name) is unique, name alone is not.
        stmt = select(TenantMcpServerRow).order_by(
            TenantMcpServerRow.name, TenantMcpServerRow.tenant_id
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def update(
        self, *, tenant_id: UUID, name: str, patch: TenantMcpServerPatch
    ) -> TenantMcpServerRecord:
        async with self._sf() as session:
            stmt = select(TenantMcpServerRow).where(
                TenantMcpServerRow.tenant_id == tenant_id,
                TenantMcpServerRow.name == name,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is None:
                raise TenantMcpServerNotFoundError(tenant_id=tenant_id, name=name)
            if patch.url is not None:
                existing.url = patch.url
            if patch.token_secret_ref is not None:
                existing.token_secret_ref = patch.token_secret_ref
            if patch.custom_headers_ref is not None:
                existing.custom_headers_ref = patch.custom_headers_ref
                existing.custom_header_names = patch.custom_header_names
            if patch.sse_read_timeout_s is not None:
                existing.sse_read_timeout_s = patch.sse_read_timeout_s
            if patch.timeout_s is not None:
                existing.timeout_s = patch.timeout_s
            if patch.enabled is not None:
                existing.enabled = patch.enabled
            existing.updated_at = _utc_now()
            # Validate the prospective record BEFORE commit: if the merged row
            # violates a cross-field invariant, _row_to_record raises and the
            # context-manager rolls back — no corrupt row is persisted (parity
            # with the in-memory store's validate-before-write semantics).
            record = _row_to_record(existing)
            await session.commit()
            return record

    async def record_probe_result(
        self,
        *,
        tenant_id: UUID,
        name: str,
        status: McpServerProbeStatus,
        probed_at: datetime,
        error: str | None = None,
    ) -> TenantMcpServerRecord:
        async with self._sf() as session:
            stmt = select(TenantMcpServerRow).where(
                TenantMcpServerRow.tenant_id == tenant_id,
                TenantMcpServerRow.name == name,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is None:
                raise TenantMcpServerNotFoundError(tenant_id=tenant_id, name=name)
            existing.last_probe_at = probed_at
            existing.last_probe_status = status
            existing.last_probe_error = error if status == "error" else None
            # Deliberately not touching updated_at — health is not a config change.
            record = _row_to_record(existing)
            await session.commit()
            return record

    async def delete(self, *, tenant_id: UUID, name: str) -> None:
        stmt = (
            sa_delete(TenantMcpServerRow)
            .where(
                TenantMcpServerRow.tenant_id == tenant_id,
                TenantMcpServerRow.name == name,
            )
            .returning(TenantMcpServerRow.id)
        )
        async with self._sf() as session:
            deleted = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
        if deleted is None:
            raise TenantMcpServerNotFoundError(tenant_id=tenant_id, name=name)

    async def count_for_catalog(self, *, catalog_id: UUID) -> int:
        # Platform-scope, cross-tenant by design — no tenant_id predicate.
        # No bypass-RLS session precedent on this store; the caller must run
        # on a platform-scope session (superuser/BYPASSRLS) or RLS filters
        # every row out. See base.py docstring.
        stmt = (
            select(func.count())
            .select_from(TenantMcpServerRow)
            .where(TenantMcpServerRow.catalog_id == catalog_id)
        )
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one())
