"""Postgres-backed :class:`McpConnectorCatalogStore` — Stream W (Mini-ADR W-1).

Platform table: every row is NULL-tenant, so callers MUST drive these methods
inside ``bypass_rls_session()`` (parity with :class:`SqlPlatformSecretStore`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.mcp_connector_catalog.base import (
    McpConnectorCatalogAlreadyExistsError,
    McpConnectorCatalogInUseError,
    McpConnectorCatalogNotFoundError,
    McpConnectorCatalogStore,
)
from expert_work.persistence.models import McpConnectorCatalogRow
from expert_work.protocol import (
    McpConnectorAuthSchema,
    McpConnectorCatalogPatch,
    McpConnectorCatalogRecord,
    McpConnectorCatalogUpsert,
    TenantPlan,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _row_to_record(row: McpConnectorCatalogRow) -> McpConnectorCatalogRecord:
    return McpConnectorCatalogRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        category=row.category,
        icon=row.icon,
        transport=row.transport,  # type: ignore[arg-type]
        url_template=row.url_template,
        auth_type=row.auth_type,  # type: ignore[arg-type]
        auth_schema=McpConnectorAuthSchema.model_validate(row.auth_schema),
        oauth_client_id=row.oauth_client_id,
        oauth_scopes=row.oauth_scopes,
        bearer_token_ref=row.bearer_token_ref,
        timeout_s=row.timeout_s,
        sse_read_timeout_s=row.sse_read_timeout_s,
        disabled_tools=list(row.disabled_tools or []),
        required_tier=TenantPlan(row.required_tier),
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


class SqlMcpConnectorCatalogStore(McpConnectorCatalogStore):
    """Postgres-backed platform MCP connector catalog (bypass-RLS sessions)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self, *, upsert: McpConnectorCatalogUpsert, actor_id: str
    ) -> McpConnectorCatalogRecord:
        now = _utc_now()
        stmt = (
            pg_insert(McpConnectorCatalogRow)
            .values(
                tenant_id=None,
                name=upsert.name,
                display_name=upsert.display_name,
                description=upsert.description,
                category=upsert.category,
                icon=upsert.icon,
                transport=upsert.transport,
                url_template=upsert.url_template,
                auth_type=upsert.auth_type,
                auth_schema=upsert.auth_schema.model_dump(),
                oauth_client_id=upsert.oauth_client_id,
                oauth_scopes=upsert.oauth_scopes,
                bearer_token_ref=upsert.bearer_token_ref,
                timeout_s=upsert.timeout_s,
                sse_read_timeout_s=upsert.sse_read_timeout_s,
                disabled_tools=list(upsert.disabled_tools),
                required_tier=upsert.required_tier.value,
                enabled=upsert.enabled,
                created_at=now,
                updated_at=now,
                updated_by=actor_id,
            )
            .returning(McpConnectorCatalogRow)
        )
        async with self._sf() as session:
            try:
                row = (await session.execute(stmt)).scalar_one()
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise McpConnectorCatalogAlreadyExistsError(name=upsert.name) from exc
            await session.refresh(row)
            return _row_to_record(row)

    async def get_by_id(self, catalog_id: UUID) -> McpConnectorCatalogRecord | None:
        async with self._sf() as session:
            row = await session.get(McpConnectorCatalogRow, catalog_id)
        return _row_to_record(row) if row is not None else None

    async def get_by_ids(
        self, catalog_ids: Sequence[UUID]
    ) -> dict[UUID, McpConnectorCatalogRecord]:
        ids = list(dict.fromkeys(catalog_ids))
        if not ids:
            return {}
        stmt = select(McpConnectorCatalogRow).where(McpConnectorCatalogRow.id.in_(ids))
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return {row.id: _row_to_record(row) for row in rows}

    async def get_by_name(self, name: str) -> McpConnectorCatalogRecord | None:
        stmt = select(McpConnectorCatalogRow).where(McpConnectorCatalogRow.name == name)
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_record(row) if row is not None else None

    async def list(self, *, category: str | None = None) -> list[McpConnectorCatalogRecord]:
        stmt = select(McpConnectorCatalogRow).order_by(McpConnectorCatalogRow.name)
        if category is not None:
            stmt = stmt.where(McpConnectorCatalogRow.category == category)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def update(
        self, *, catalog_id: UUID, patch: McpConnectorCatalogPatch
    ) -> McpConnectorCatalogRecord:
        return (await self.update_with_previous(catalog_id=catalog_id, patch=patch))[1]

    async def update_with_previous(
        self, *, catalog_id: UUID, patch: McpConnectorCatalogPatch
    ) -> tuple[McpConnectorCatalogRecord, McpConnectorCatalogRecord]:
        async with self._sf() as session:
            # FOR UPDATE: a concurrent update waits, then reads our result as
            # its ``before`` (B-248).
            existing = await session.get(McpConnectorCatalogRow, catalog_id, with_for_update=True)
            if existing is None:
                raise McpConnectorCatalogNotFoundError(catalog_id=catalog_id)
            before = _row_to_record(existing)
            if patch.display_name is not None:
                existing.display_name = patch.display_name
            if patch.description is not None:
                existing.description = patch.description
            if patch.category is not None:
                existing.category = patch.category
            if patch.icon is not None:
                existing.icon = patch.icon
            if patch.url_template is not None:
                existing.url_template = patch.url_template
            if patch.auth_schema is not None:
                existing.auth_schema = patch.auth_schema.model_dump()
            if patch.bearer_token_ref is not None:
                existing.bearer_token_ref = patch.bearer_token_ref
            if patch.timeout_s is not None:
                existing.timeout_s = patch.timeout_s
            if patch.sse_read_timeout_s is not None:
                existing.sse_read_timeout_s = patch.sse_read_timeout_s
            if patch.disabled_tools is not None:
                existing.disabled_tools = list(patch.disabled_tools)
            if patch.required_tier is not None:
                existing.required_tier = patch.required_tier.value
            if patch.enabled is not None:
                existing.enabled = patch.enabled
            existing.updated_at = _utc_now()
            # Validate the prospective record BEFORE commit: if the merged row
            # violates a cross-field invariant (e.g. bearer auth without a
            # secret field), _row_to_record raises and the context-manager
            # rolls back — no corrupt row is persisted.
            record = _row_to_record(existing)
            await session.commit()
            return before, record

    async def delete(self, catalog_id: UUID) -> None:
        stmt = (
            sa_delete(McpConnectorCatalogRow)
            .where(McpConnectorCatalogRow.id == catalog_id)
            .returning(McpConnectorCatalogRow.id)
        )
        async with self._sf() as session:
            # Both FKs guarding this row (tenant_mcp_server.catalog_id,
            # mcp_oauth_connection.catalog_id — deletion-hygiene PR2 Task 8)
            # are ON DELETE RESTRICT, which Postgres checks immediately —
            # non-deferrable constraints fire while the DELETE statement
            # itself executes, not later at commit — so the guard has to
            # wrap ``execute`` too, not just ``commit``.
            try:
                deleted = (await session.execute(stmt)).scalar_one_or_none()
            except IntegrityError as exc:
                await session.rollback()
                raise McpConnectorCatalogInUseError(catalog_id=catalog_id) from exc
            if deleted is None:
                # Roll back the no-op DELETE before raising NotFound.
                await session.rollback()
                raise McpConnectorCatalogNotFoundError(catalog_id=catalog_id)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise McpConnectorCatalogInUseError(catalog_id=catalog_id) from exc
