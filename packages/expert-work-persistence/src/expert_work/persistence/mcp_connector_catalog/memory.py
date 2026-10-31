"""In-memory :class:`McpConnectorCatalogStore` — Stream W (Mini-ADR W-1)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from expert_work.persistence.mcp_connector_catalog.base import (
    McpConnectorCatalogAlreadyExistsError,
    McpConnectorCatalogNotFoundError,
    McpConnectorCatalogStore,
)
from expert_work.protocol import (
    McpConnectorCatalogPatch,
    McpConnectorCatalogRecord,
    McpConnectorCatalogUpsert,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryMcpConnectorCatalogStore(McpConnectorCatalogStore):
    """Dict-backed catalog store keyed by ``id``; lock-guarded."""

    def __init__(self) -> None:
        self._rows: dict[UUID, McpConnectorCatalogRecord] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, *, upsert: McpConnectorCatalogUpsert, actor_id: str
    ) -> McpConnectorCatalogRecord:
        async with self._lock:
            if any(r.name == upsert.name for r in self._rows.values()):
                raise McpConnectorCatalogAlreadyExistsError(name=upsert.name)
            now = _utc_now()
            record = McpConnectorCatalogRecord(
                id=uuid4(),
                tenant_id=None,
                name=upsert.name,
                display_name=upsert.display_name,
                description=upsert.description,
                category=upsert.category,
                icon=upsert.icon,
                transport=upsert.transport,
                url_template=upsert.url_template,
                auth_type=upsert.auth_type,
                auth_schema=upsert.auth_schema,
                oauth_client_id=upsert.oauth_client_id,
                oauth_scopes=upsert.oauth_scopes,
                bearer_token_ref=upsert.bearer_token_ref,
                timeout_s=upsert.timeout_s,
                sse_read_timeout_s=upsert.sse_read_timeout_s,
                disabled_tools=list(upsert.disabled_tools),
                required_tier=upsert.required_tier,
                enabled=upsert.enabled,
                created_at=now,
                updated_at=now,
                updated_by=actor_id,
            )
            self._rows[record.id] = record
            return record

    async def get_by_id(self, catalog_id: UUID) -> McpConnectorCatalogRecord | None:
        async with self._lock:
            return self._rows.get(catalog_id)

    async def get_by_ids(
        self, catalog_ids: Sequence[UUID]
    ) -> dict[UUID, McpConnectorCatalogRecord]:
        wanted = set(catalog_ids)
        async with self._lock:
            return {cid: self._rows[cid] for cid in wanted if cid in self._rows}

    async def get_by_name(self, name: str) -> McpConnectorCatalogRecord | None:
        async with self._lock:
            return next((r for r in self._rows.values() if r.name == name), None)

    async def list(self, *, category: str | None = None) -> list[McpConnectorCatalogRecord]:
        async with self._lock:
            rows = [r for r in self._rows.values() if category is None or r.category == category]
        return sorted(rows, key=lambda r: r.name)

    async def update(
        self, *, catalog_id: UUID, patch: McpConnectorCatalogPatch
    ) -> McpConnectorCatalogRecord:
        return (await self.update_with_previous(catalog_id=catalog_id, patch=patch))[1]

    async def update_with_previous(
        self, *, catalog_id: UUID, patch: McpConnectorCatalogPatch
    ) -> tuple[McpConnectorCatalogRecord, McpConnectorCatalogRecord]:
        async with self._lock:
            existing = self._rows.get(catalog_id)
            if existing is None:
                raise McpConnectorCatalogNotFoundError(catalog_id=catalog_id)
            # patch field == None means "leave unchanged"; name/transport are
            # immutable post-create (re-create to change them).
            changes: dict[str, object] = {"updated_at": _utc_now()}
            if patch.display_name is not None:
                changes["display_name"] = patch.display_name
            if patch.description is not None:
                changes["description"] = patch.description
            if patch.category is not None:
                changes["category"] = patch.category
            if patch.icon is not None:
                changes["icon"] = patch.icon
            if patch.url_template is not None:
                changes["url_template"] = patch.url_template
            if patch.auth_schema is not None:
                changes["auth_schema"] = patch.auth_schema
            if patch.bearer_token_ref is not None:
                changes["bearer_token_ref"] = patch.bearer_token_ref
            if patch.timeout_s is not None:
                changes["timeout_s"] = patch.timeout_s
            if patch.sse_read_timeout_s is not None:
                changes["sse_read_timeout_s"] = patch.sse_read_timeout_s
            if patch.disabled_tools is not None:
                changes["disabled_tools"] = list(patch.disabled_tools)
            if patch.required_tier is not None:
                changes["required_tier"] = patch.required_tier
            if patch.enabled is not None:
                changes["enabled"] = patch.enabled
            # Re-validate the merged row (model_copy doesn't run validators) so
            # a patch can't slip a cross-field-invalid record (parity with SQL).
            updated = McpConnectorCatalogRecord.model_validate(
                existing.model_copy(update=changes).model_dump()
            )
            self._rows[catalog_id] = updated
            return existing, updated

    async def delete(self, catalog_id: UUID) -> None:
        async with self._lock:
            if catalog_id not in self._rows:
                raise McpConnectorCatalogNotFoundError(catalog_id=catalog_id)
            del self._rows[catalog_id]
