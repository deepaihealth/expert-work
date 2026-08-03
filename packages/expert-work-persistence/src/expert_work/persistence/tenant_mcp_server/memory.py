"""In-memory :class:`TenantMcpServerStore` — Stream V."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

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


def _now() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryTenantMcpServerStore(TenantMcpServerStore):
    """Dict-backed store keyed by ``(tenant_id, name)``; lock-guarded."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str], TenantMcpServerRecord] = {}
        self._lock = asyncio.Lock()

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
        async with self._lock:
            key = (tenant_id, name)
            if key in self._rows:
                raise TenantMcpServerAlreadyExistsError(tenant_id=tenant_id, name=name)
            now = _now()
            record = TenantMcpServerRecord(
                id=uuid4(),
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
            self._rows[key] = record
            return record

    async def get(self, *, tenant_id: UUID, name: str) -> TenantMcpServerRecord | None:
        async with self._lock:
            return self._rows.get((tenant_id, name))

    async def list_for_tenant(self, *, tenant_id: UUID) -> list[TenantMcpServerRecord]:
        async with self._lock:
            rows = [r for (tid, _), r in self._rows.items() if tid == tenant_id]
        return sorted(rows, key=lambda r: r.name)

    async def list_all_tenants(self) -> list[TenantMcpServerRecord]:
        # W4 — no tenant filter; (name, tenant_id) ordering matching the SQL
        # implementation byte-for-byte (uuid int order == Postgres byte order).
        async with self._lock:
            rows = list(self._rows.values())
        return sorted(rows, key=lambda r: (r.name, r.tenant_id.int))

    async def update(
        self, *, tenant_id: UUID, name: str, patch: TenantMcpServerPatch
    ) -> TenantMcpServerRecord:
        async with self._lock:
            key = (tenant_id, name)
            existing = self._rows.get(key)
            if existing is None:
                raise TenantMcpServerNotFoundError(tenant_id=tenant_id, name=name)
            # patch field == None means "leave unchanged" (not "clear");
            # auth-type/token changes go via delete+recreate.
            changes: dict[str, object] = {"updated_at": _now()}
            if patch.url is not None:
                changes["url"] = patch.url
            if patch.token_secret_ref is not None:
                changes["token_secret_ref"] = patch.token_secret_ref
            if patch.custom_headers_ref is not None:
                changes["custom_headers_ref"] = patch.custom_headers_ref
                changes["custom_header_names"] = patch.custom_header_names
            if patch.sse_read_timeout_s is not None:
                changes["sse_read_timeout_s"] = patch.sse_read_timeout_s
            if patch.timeout_s is not None:
                changes["timeout_s"] = patch.timeout_s
            if patch.enabled is not None:
                changes["enabled"] = patch.enabled
            updated = TenantMcpServerRecord.model_validate(
                existing.model_copy(update=changes).model_dump()
            )
            self._rows[key] = updated
            return updated

    async def record_probe_result(
        self,
        *,
        tenant_id: UUID,
        name: str,
        status: McpServerProbeStatus,
        probed_at: datetime,
        error: str | None = None,
    ) -> TenantMcpServerRecord:
        async with self._lock:
            key = (tenant_id, name)
            existing = self._rows.get(key)
            if existing is None:
                raise TenantMcpServerNotFoundError(tenant_id=tenant_id, name=name)
            # Health columns only — updated_at is left untouched (not a config change).
            updated = existing.model_copy(
                update={
                    "last_probe_at": probed_at,
                    "last_probe_status": status,
                    "last_probe_error": error if status == "error" else None,
                }
            )
            self._rows[key] = updated
            return updated

    async def delete(self, *, tenant_id: UUID, name: str) -> None:
        async with self._lock:
            key = (tenant_id, name)
            if key not in self._rows:
                raise TenantMcpServerNotFoundError(tenant_id=tenant_id, name=name)
            del self._rows[key]

    async def count_for_catalog(self, *, catalog_id: UUID) -> int:
        async with self._lock:
            return sum(1 for r in self._rows.values() if r.catalog_id == catalog_id)
