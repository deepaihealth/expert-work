"""Unit tests for :class:`InMemoryTenantConfigStore.create` — Stream P (P-1/P-3)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from expert_work.persistence.tenant_config.base import (
    TenantConfigAlreadyExistsError,
    TenantConfigNotFoundError,
)
from expert_work.persistence.tenant_config.memory import InMemoryTenantConfigStore
from expert_work.protocol import TenantConfigPatch, TenantPlan


@pytest.mark.asyncio
async def test_create_writes_first_row_with_defaults() -> None:
    store = InMemoryTenantConfigStore()
    tenant_id = uuid4()

    record = await store.create(
        tenant_id=tenant_id,
        display_name="Acme Inc",
        actor_id="bootstrap",
    )

    assert record.tenant_id == tenant_id
    assert record.display_name == "Acme Inc"
    assert record.plan is TenantPlan.FREE  # default
    assert record.updated_by == "bootstrap"
    # Every other field falls back to its baseline default.
    assert record.model_credentials_ref == {}
    assert record.credentials_mode == "platform"
    # And the row is now readable via get().
    assert (await store.get(tenant_id=tenant_id)) == record


@pytest.mark.asyncio
async def test_create_honours_explicit_plan() -> None:
    store = InMemoryTenantConfigStore()
    tenant_id = uuid4()

    record = await store.create(
        tenant_id=tenant_id,
        display_name="Pro Co",
        plan=TenantPlan.PRO,
        actor_id="admin",
    )

    assert record.plan is TenantPlan.PRO


@pytest.mark.asyncio
async def test_create_rejects_existing_tenant() -> None:
    store = InMemoryTenantConfigStore()
    tenant_id = uuid4()
    await store.create(tenant_id=tenant_id, display_name="First", actor_id="a")

    with pytest.raises(TenantConfigAlreadyExistsError) as exc:
        await store.create(tenant_id=tenant_id, display_name="Second", actor_id="b")
    assert exc.value.tenant_id == tenant_id
    # The original row is untouched (no silent overwrite, unlike upsert).
    fetched = await store.get(tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.display_name == "First"


@pytest.mark.asyncio
async def test_created_tenant_status_is_active() -> None:
    store = InMemoryTenantConfigStore()
    tenant_id = uuid4()
    record = await store.create(tenant_id=tenant_id, display_name="Acme", actor_id="a")
    assert record.status == "active"


@pytest.mark.asyncio
async def test_set_status_suspends_tenant() -> None:
    store = InMemoryTenantConfigStore()
    tenant_id = uuid4()
    await store.create(tenant_id=tenant_id, display_name="Acme", actor_id="a")

    updated = await store.set_status(tenant_id=tenant_id, status="suspended", actor_id="ops")

    assert updated.status == "suspended"
    assert updated.updated_by == "ops"
    fetched = await store.get(tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.status == "suspended"


@pytest.mark.asyncio
async def test_set_status_unknown_tenant_raises() -> None:
    store = InMemoryTenantConfigStore()
    missing = uuid4()
    with pytest.raises(TenantConfigNotFoundError) as exc:
        await store.set_status(tenant_id=missing, status="suspended", actor_id="ops")
    assert exc.value.tenant_id == missing


@pytest.mark.asyncio
async def test_list_all_empty() -> None:
    store = InMemoryTenantConfigStore()
    assert await store.list_all() == []


@pytest.mark.asyncio
async def test_list_all_returns_created_tenants() -> None:
    store = InMemoryTenantConfigStore()
    a = await store.create(tenant_id=uuid4(), display_name="Acme", actor_id="sys")
    b = await store.create(tenant_id=uuid4(), display_name="Beta", actor_id="sys")
    got = {r.tenant_id for r in await store.list_all()}
    assert got == {a.tenant_id, b.tenant_id}


@pytest.mark.asyncio
async def test_list_all_paginates() -> None:
    store = InMemoryTenantConfigStore()
    for i in range(3):
        await store.create(tenant_id=uuid4(), display_name=f"T{i}", actor_id="sys")
    assert len(await store.list_all(limit=2, offset=0)) == 2
    assert len(await store.list_all(limit=2, offset=2)) == 1


@pytest.mark.asyncio
async def test_allow_custom_mcp_servers_defaults_true() -> None:
    store = InMemoryTenantConfigStore()
    record = await store.create(tenant_id=uuid4(), display_name="Acme", actor_id="sys")
    assert record.allow_custom_mcp_servers is True


@pytest.mark.asyncio
async def test_allow_custom_mcp_servers_patch_round_trips() -> None:
    store = InMemoryTenantConfigStore()
    tid = uuid4()
    await store.create(tenant_id=tid, display_name="Acme", actor_id="sys")
    updated = await store.upsert(
        tenant_id=tid,
        patch=TenantConfigPatch(allow_custom_mcp_servers=False),
        actor_id="ops",
    )
    assert updated.allow_custom_mcp_servers is False
    got = await store.get(tenant_id=tid)
    assert got is not None and got.allow_custom_mcp_servers is False


@pytest.mark.asyncio
async def test_allow_custom_mcp_servers_set_false_on_first_upsert() -> None:
    store = InMemoryTenantConfigStore()
    tid = uuid4()
    record = await store.upsert(
        tenant_id=tid,
        patch=TenantConfigPatch(display_name="Acme", allow_custom_mcp_servers=False),
        actor_id="sys",
    )
    assert record.allow_custom_mcp_servers is False


@pytest.mark.asyncio
async def test_skill_evolution_judge_sample_pct_defaults_and_round_trips() -> None:
    """SE-16 (SE-A45) — implicit judge sample rate defaults 5, patches through."""
    store = InMemoryTenantConfigStore()
    tid = uuid4()
    created = await store.create(tenant_id=tid, display_name="Acme", actor_id="sys")
    assert created.skill_evolution_judge_sample_pct == 5

    updated = await store.upsert(
        tenant_id=tid,
        patch=TenantConfigPatch(skill_evolution_judge_sample_pct=25),
        actor_id="ops",
    )
    assert updated.skill_evolution_judge_sample_pct == 25
    got = await store.get(tenant_id=tid)
    assert got is not None and got.skill_evolution_judge_sample_pct == 25


@pytest.mark.asyncio
async def test_memory_predictive_review_enabled_defaults_and_round_trips() -> None:
    """P5b-2b ⑦ — predictive review opt-in defaults False, patches through.

    Closes the activation-gap Critical finding on 7a8e9155 (P5b-2b T4) for
    the in-memory store's upsert path (mirrors the SQL store round-trip).
    """
    store = InMemoryTenantConfigStore()
    tid = uuid4()
    created = await store.create(tenant_id=tid, display_name="Acme", actor_id="sys")
    assert created.memory_predictive_review_enabled is False

    updated = await store.upsert(
        tenant_id=tid,
        patch=TenantConfigPatch(memory_predictive_review_enabled=True),
        actor_id="ops",
    )
    assert updated.memory_predictive_review_enabled is True
    got = await store.get(tenant_id=tid)
    assert got is not None and got.memory_predictive_review_enabled is True


# ---------------------------------------------------------------------------
# BUG-1(2026-08-24)mcp_allowlist 原子 add/remove —— enable/disable 端点原先
# 「缓存读→改→整表覆盖写」在多副本下互相抹名字;原子操作把合并下放到 store。


async def _seeded_store(tenant_id: UUID) -> InMemoryTenantConfigStore:
    store = InMemoryTenantConfigStore()
    await store.create(tenant_id=tenant_id, display_name="Acme", actor_id="bootstrap")
    return store


@pytest.mark.asyncio
async def test_add_mcp_allowlist_name_appends_and_reports_changed() -> None:
    tenant_id = uuid4()
    store = await _seeded_store(tenant_id)

    record, changed = await store.add_mcp_allowlist_name(
        tenant_id=tenant_id, name="amap-maps", actor_id="op"
    )
    assert changed is True
    assert record.mcp_allowlist == ["amap-maps"]
    assert record.updated_by == "op"
    fetched = await store.get(tenant_id=tenant_id)
    assert fetched is not None and fetched.mcp_allowlist == ["amap-maps"]


@pytest.mark.asyncio
async def test_add_mcp_allowlist_name_is_idempotent() -> None:
    tenant_id = uuid4()
    store = await _seeded_store(tenant_id)
    await store.add_mcp_allowlist_name(tenant_id=tenant_id, name="a", actor_id="op")

    record, changed = await store.add_mcp_allowlist_name(
        tenant_id=tenant_id, name="a", actor_id="op2"
    )
    assert changed is False
    assert record.mcp_allowlist == ["a"]


@pytest.mark.asyncio
async def test_add_preserves_existing_names() -> None:
    """丢失更新回归:后一个 add 不得抹掉先前的名字。"""
    tenant_id = uuid4()
    store = await _seeded_store(tenant_id)
    await store.add_mcp_allowlist_name(tenant_id=tenant_id, name="deep", actor_id="op")

    record, _ = await store.add_mcp_allowlist_name(tenant_id=tenant_id, name="amap", actor_id="op")
    assert record.mcp_allowlist == ["deep", "amap"]


@pytest.mark.asyncio
async def test_remove_mcp_allowlist_name_removes_and_is_idempotent() -> None:
    tenant_id = uuid4()
    store = await _seeded_store(tenant_id)
    await store.add_mcp_allowlist_name(tenant_id=tenant_id, name="a", actor_id="op")
    await store.add_mcp_allowlist_name(tenant_id=tenant_id, name="b", actor_id="op")

    record, changed = await store.remove_mcp_allowlist_name(
        tenant_id=tenant_id, name="a", actor_id="op"
    )
    assert changed is True
    assert record.mcp_allowlist == ["b"]

    record, changed = await store.remove_mcp_allowlist_name(
        tenant_id=tenant_id, name="a", actor_id="op"
    )
    assert changed is False
    assert record.mcp_allowlist == ["b"]


@pytest.mark.asyncio
async def test_add_on_missing_tenant_raises_not_found() -> None:
    store = InMemoryTenantConfigStore()
    with pytest.raises(TenantConfigNotFoundError):
        await store.add_mcp_allowlist_name(tenant_id=uuid4(), name="x", actor_id="op")
    with pytest.raises(TenantConfigNotFoundError):
        await store.remove_mcp_allowlist_name(tenant_id=uuid4(), name="x", actor_id="op")
