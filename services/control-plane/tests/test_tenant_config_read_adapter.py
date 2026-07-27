"""二期 P1.2 —— ServiceBackedTenantConfigStore:recall 路径的 tenant_config
读走 TenantConfigService 的现有缓存,不再每次召回打一条 DB 读。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from control_plane.audit import build_default_audit_logger
from control_plane.tenancy.tenant_config import (
    ServiceBackedTenantConfigStore,
    TenantConfigService,
)
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.protocol import TenantConfigPatch, TenantConfigRecord


class _CountingStore(InMemoryTenantConfigStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord | None:
        self.get_calls += 1
        return await super().get(tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_get_hits_service_cache_not_store() -> None:
    store = _CountingStore()
    tenant_id = uuid4()
    # In-memory store requires display_name on the first upsert.
    await store.upsert(
        tenant_id=tenant_id,
        patch=TenantConfigPatch(display_name="t", memory_recall_mode="vector"),
        actor_id="t",
    )
    service = TenantConfigService(
        store=store, audit_logger=build_default_audit_logger(), ttl_s=60.0
    )
    adapter = ServiceBackedTenantConfigStore(service=service)

    first = await adapter.get(tenant_id=tenant_id)
    second = await adapter.get(tenant_id=tenant_id)
    assert first is not None and first.memory_recall_mode == "vector"
    assert second is not None
    assert store.get_calls == 1  # 第二次命中 service 缓存,没打 store


@pytest.mark.asyncio
async def test_get_missing_row_returns_none_not_raise() -> None:
    """store 接口约定 miss 返回 None;service 的 NotConfiguredError 必须被
    适配器吞掉转 None —— recall 节点靠 None 走默认 hybrid。"""
    service = TenantConfigService(
        store=InMemoryTenantConfigStore(),
        audit_logger=build_default_audit_logger(),
        ttl_s=60.0,
    )
    adapter = ServiceBackedTenantConfigStore(service=service)
    assert await adapter.get(tenant_id=uuid4()) is None
