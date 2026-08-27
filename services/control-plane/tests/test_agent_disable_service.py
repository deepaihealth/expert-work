"""Unit tests for :class:`AgentDisableService` — Stream RT-4 (RT-ADR-16).

Mirrors the TTL-cache / invalidate / fail-open contract of
:class:`~control_plane.tenant_status.TenantStatusService`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from control_plane.agent_disable_status import AgentDisableService
from expert_work.persistence import InMemoryAgentDisableStore


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_fail_open_when_no_row() -> None:
    store = InMemoryAgentDisableStore()
    svc = AgentDisableService(store=store)
    # No row → not disabled (fail-open; enforcement is a deliberate admin act).
    assert await svc.is_disabled(uuid4(), "support-bot") is False


@pytest.mark.asyncio
async def test_reads_disabled_flag() -> None:
    store = InMemoryAgentDisableStore()
    tenant = uuid4()
    await store.set_disabled(
        tenant_id=tenant, agent_name="a", disabled=True, reason=None, disabled_by="admin"
    )
    svc = AgentDisableService(store=store)
    assert await svc.is_disabled(tenant, "a") is True


@pytest.mark.asyncio
async def test_ttl_cache_serves_stale_until_expiry() -> None:
    store = InMemoryAgentDisableStore()
    tenant = uuid4()
    clock = _FakeClock()
    svc = AgentDisableService(store=store, ttl_seconds=30.0, clock=clock)

    # Cold read: not disabled, cached for 30s.
    assert await svc.is_disabled(tenant, "a") is False
    # Now disable in the store; within the TTL the cache still says False.
    await store.set_disabled(
        tenant_id=tenant, agent_name="a", disabled=True, reason=None, disabled_by="admin"
    )
    clock.t = 29.0
    assert await svc.is_disabled(tenant, "a") is False
    # Past the TTL the cache reloads and sees the disable.
    clock.t = 31.0
    assert await svc.is_disabled(tenant, "a") is True


@pytest.mark.asyncio
async def test_invalidate_forces_reload() -> None:
    store = InMemoryAgentDisableStore()
    tenant = uuid4()
    clock = _FakeClock()
    svc = AgentDisableService(store=store, ttl_seconds=30.0, clock=clock)

    assert await svc.is_disabled(tenant, "a") is False
    await store.set_disabled(
        tenant_id=tenant, agent_name="a", disabled=True, reason=None, disabled_by="admin"
    )
    # Without waiting for the TTL, invalidate → immediate effect.
    svc.invalidate(tenant, "a")
    assert await svc.is_disabled(tenant, "a") is True


@pytest.mark.asyncio
async def test_invalidate_tenant_forces_reload_for_all_agents_of_that_tenant() -> None:
    # PR-E3b — the invalidation-bus ``agent_disable`` event carries only the
    # tenant, so the whole tenant's cached flags drop; other tenants keep
    # their (stale-tolerant) cache entries.
    store = InMemoryAgentDisableStore()
    tenant = uuid4()
    other_tenant = uuid4()
    clock = _FakeClock()
    svc = AgentDisableService(store=store, ttl_seconds=30.0, clock=clock)

    # Warm the cache for two agents of ``tenant`` and one of ``other_tenant``.
    assert await svc.is_disabled(tenant, "a") is False
    assert await svc.is_disabled(tenant, "b") is False
    assert await svc.is_disabled(other_tenant, "a") is False
    # Disable everything in the store; within the TTL the cache hides it.
    for tid, name in ((tenant, "a"), (tenant, "b"), (other_tenant, "a")):
        await store.set_disabled(
            tenant_id=tid, agent_name=name, disabled=True, reason=None, disabled_by="admin"
        )
    svc.invalidate_tenant(tenant)
    # Both of the tenant's agents reload immediately …
    assert await svc.is_disabled(tenant, "a") is True
    assert await svc.is_disabled(tenant, "b") is True
    # … while the other tenant still serves its cached (stale) value.
    assert await svc.is_disabled(other_tenant, "a") is False


@pytest.mark.asyncio
async def test_cache_is_keyed_per_tenant_and_agent() -> None:
    store = InMemoryAgentDisableStore()
    tenant = uuid4()
    other_tenant = uuid4()
    await store.set_disabled(
        tenant_id=tenant, agent_name="a", disabled=True, reason=None, disabled_by="admin"
    )
    svc = AgentDisableService(store=store)
    assert await svc.is_disabled(tenant, "a") is True
    # Different agent name / different tenant are independent (not disabled).
    assert await svc.is_disabled(tenant, "b") is False
    assert await svc.is_disabled(other_tenant, "a") is False
