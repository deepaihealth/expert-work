"""In-memory :class:`PlatformSecretStore` — Stream P (Mini-ADR P-7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.platform_secrets.base import PlatformSecretStore
from expert_work.protocol import (
    PlatformProviderSecretRecord,
    PlatformToolSecretRecord,
    Provider,
    TenantProviderSecretRecord,
    TenantToolSecretRecord,
    Tool,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryPlatformSecretStore(PlatformSecretStore):
    """Dict-backed store; lock-guarded for asyncio safety."""

    def __init__(self) -> None:
        # Y-MK — keyed by (provider, key_id) so a provider can hold many keys.
        self._providers: dict[tuple[Provider, str], PlatformProviderSecretRecord] = {}
        self._tools: dict[Tool, PlatformToolSecretRecord] = {}
        self._tenant_providers: dict[tuple[UUID, Provider], TenantProviderSecretRecord] = {}
        self._tenant_tools: dict[tuple[UUID, Tool], TenantToolSecretRecord] = {}
        self._lock = asyncio.Lock()

    async def list_providers(self) -> list[PlatformProviderSecretRecord]:
        async with self._lock:
            return list(self._providers.values())

    async def get_provider(
        self, provider: Provider, key_id: str = "default"
    ) -> PlatformProviderSecretRecord | None:
        async with self._lock:
            return self._providers.get((provider, key_id))

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
        now = _now()
        async with self._lock:
            existing = self._providers.get((provider, key_id))
            created_at = existing.created_at if existing is not None else now
            record = PlatformProviderSecretRecord(
                provider=provider,
                key_id=key_id,
                secret_ref=secret_ref,
                enabled=enabled,
                priority=priority,
                base_url=base_url,
                base_url_unverified=base_url_unverified,
                created_at=created_at,
                updated_at=now,
                updated_by=actor_id,
            )
            self._providers[(provider, key_id)] = record
            return record

    async def delete_provider(self, provider: Provider, key_id: str = "default") -> bool:
        async with self._lock:
            return self._providers.pop((provider, key_id), None) is not None

    async def list_tools(self) -> list[PlatformToolSecretRecord]:
        async with self._lock:
            return list(self._tools.values())

    async def get_tool(self, tool: Tool) -> PlatformToolSecretRecord | None:
        async with self._lock:
            return self._tools.get(tool)

    async def upsert_tool(
        self,
        *,
        tool: Tool,
        secret_ref: str,
        enabled: bool,
        actor_id: str,
    ) -> PlatformToolSecretRecord:
        now = _now()
        async with self._lock:
            existing = self._tools.get(tool)
            created_at = existing.created_at if existing is not None else now
            record = PlatformToolSecretRecord(
                tool=tool,
                secret_ref=secret_ref,
                enabled=enabled,
                created_at=created_at,
                updated_at=now,
                updated_by=actor_id,
            )
            self._tools[tool] = record
            return record

    async def delete_tool(self, tool: Tool) -> bool:
        async with self._lock:
            return self._tools.pop(tool, None) is not None

    # --- per-tenant overrides (Stream HX-8) ---------------------------

    async def list_tenant_providers(
        self, tenant_id: UUID | None = None
    ) -> list[TenantProviderSecretRecord]:
        async with self._lock:
            return [
                r
                for r in self._tenant_providers.values()
                if tenant_id is None or r.tenant_id == tenant_id
            ]

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
        now = _now()
        async with self._lock:
            existing = self._tenant_providers.get((tenant_id, provider))
            created_at = existing.created_at if existing is not None else now
            record = TenantProviderSecretRecord(
                tenant_id=tenant_id,
                provider=provider,
                secret_ref=secret_ref,
                enabled=enabled,
                base_url=base_url,
                base_url_unverified=base_url_unverified,
                created_at=created_at,
                updated_at=now,
                updated_by=actor_id,
            )
            self._tenant_providers[(tenant_id, provider)] = record
            return record

    async def delete_tenant_provider(self, *, tenant_id: UUID, provider: Provider) -> bool:
        async with self._lock:
            return self._tenant_providers.pop((tenant_id, provider), None) is not None

    async def list_tenant_tools(
        self, tenant_id: UUID | None = None
    ) -> list[TenantToolSecretRecord]:
        async with self._lock:
            return [
                r
                for r in self._tenant_tools.values()
                if tenant_id is None or r.tenant_id == tenant_id
            ]

    async def upsert_tenant_tool(
        self,
        *,
        tenant_id: UUID,
        tool: Tool,
        secret_ref: str,
        enabled: bool,
        actor_id: str,
    ) -> TenantToolSecretRecord:
        now = _now()
        async with self._lock:
            existing = self._tenant_tools.get((tenant_id, tool))
            created_at = existing.created_at if existing is not None else now
            record = TenantToolSecretRecord(
                tenant_id=tenant_id,
                tool=tool,
                secret_ref=secret_ref,
                enabled=enabled,
                created_at=created_at,
                updated_at=now,
                updated_by=actor_id,
            )
            self._tenant_tools[(tenant_id, tool)] = record
            return record

    async def delete_tenant_tool(self, *, tenant_id: UUID, tool: Tool) -> bool:
        async with self._lock:
            return self._tenant_tools.pop((tenant_id, tool), None) is not None
