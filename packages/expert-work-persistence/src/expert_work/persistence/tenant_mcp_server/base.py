"""Persistence Protocol for the tenant MCP server registry — Stream V."""

from __future__ import annotations

import abc
from datetime import datetime
from uuid import UUID

from expert_work.protocol import (
    McpServerAuthType,
    McpServerProbeStatus,
    McpServerTransport,
    TenantMcpServerPatch,
    TenantMcpServerRecord,
)


class TenantMcpServerNotFoundError(Exception):
    """No ``tenant_mcp_server`` row for the requested (tenant, name)."""

    def __init__(self, *, tenant_id: UUID, name: str) -> None:
        super().__init__(f"tenant_mcp_server not found: tenant_id={tenant_id} name={name!r}")
        self.tenant_id = tenant_id
        self.name = name


class TenantMcpServerAlreadyExistsError(Exception):
    """A ``tenant_mcp_server`` row already exists for (tenant, name)."""

    def __init__(self, *, tenant_id: UUID, name: str) -> None:
        super().__init__(f"tenant_mcp_server already exists: tenant_id={tenant_id} name={name!r}")
        self.tenant_id = tenant_id
        self.name = name


class TenantMcpServerStore(abc.ABC):
    """CRUD for tenant-registered remote MCP servers."""

    @abc.abstractmethod
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
        """Insert a new server row. Raises
        :class:`TenantMcpServerAlreadyExistsError` on (tenant, name) conflict.

        ``catalog_id`` (Stream W) binds the row to a platform catalog entry;
        ``None`` (the default) = an off-catalog custom registration."""

    @abc.abstractmethod
    async def get(self, *, tenant_id: UUID, name: str) -> TenantMcpServerRecord | None:
        """Return the row, or None if absent."""

    @abc.abstractmethod
    async def list_for_tenant(self, *, tenant_id: UUID) -> list[TenantMcpServerRecord]:
        """Return all rows for the tenant, ordered by ``name``."""

    @abc.abstractmethod
    async def list_all_tenants(self) -> list[TenantMcpServerRecord]:
        """Every tenant's rows, ordered by ``(name, tenant_id)`` — the W4
        ``tenant_id=*`` aggregate. Caller MUST wrap the call in
        ``bypass_rls_session()`` / ``applied_scope(CrossTenant)``."""

    @abc.abstractmethod
    async def update(
        self, *, tenant_id: UUID, name: str, patch: TenantMcpServerPatch
    ) -> TenantMcpServerRecord:
        """Apply a partial update. Raises
        :class:`TenantMcpServerNotFoundError` if absent."""

    @abc.abstractmethod
    async def record_probe_result(
        self,
        *,
        tenant_id: UUID,
        name: str,
        status: McpServerProbeStatus,
        probed_at: datetime,
        error: str | None = None,
    ) -> TenantMcpServerRecord:
        """Persist the latest connectivity-probe result (#2) on the row.

        Sets ``last_probe_at`` / ``last_probe_status`` / ``last_probe_error``
        only — it does NOT bump ``updated_at`` (a probe is not a config change).
        Raises :class:`TenantMcpServerNotFoundError` if absent."""

    @abc.abstractmethod
    async def delete(self, *, tenant_id: UUID, name: str) -> None:
        """Delete the row. Raises
        :class:`TenantMcpServerNotFoundError` if absent."""

    @abc.abstractmethod
    async def count_for_catalog(self, *, catalog_id: UUID) -> int:
        """Count instantiated rows for a catalog connector, across EVERY tenant.

        Platform-scope caller only (MCP catalog delete-guard order-of-operations
        fix, Task 8 review I-1) — no ``tenant_id`` predicate by design. Lets the
        DELETE endpoint check in-use status BEFORE any destructive action
        (including the ``force`` OAuth-connection cascade), instead of relying
        solely on ``McpConnectorCatalogStore.delete``'s post-hoc
        ``IntegrityError`` mapping. No existing bypass-RLS session precedent on
        this store, so a SQL implementation runs under the caller's ordinary
        session; the caller must be a platform-scope path (e.g. a
        superuser/BYPASSRLS DB connection) or RLS filters every row out."""
