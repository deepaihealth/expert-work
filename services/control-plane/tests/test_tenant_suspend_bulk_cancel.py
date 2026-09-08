"""Tenant-suspend bulk-cancel backfill — Stream RT-4 (RT-ADR-17).

Suspending a tenant previously only rejected NEW runs; it now also terminates
the tenant's in-flight runs. Verified end-to-end through ``POST
/v1/tenants/{id}/deactivate`` (system_admin) with a seeded RUNNING run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api.tenants import _bulk_cancel_tenant_runs
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.protocol import AuditAction, AuditQuery, Role
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunManager,
    RunStatus,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


class _Ctx:
    def __init__(
        self,
        *,
        client: AsyncClient,
        app: object,
        sys_admin_id: UUID,
        run_store: InMemoryRunStore,
        audit_store: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.app = app
        self.sys_admin_id = sys_admin_id
        self.run_store = run_store
        self.audit_store = audit_store


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    settings = Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    threads = InMemoryThreadMetaStore()
    run_store = InMemoryRunStore(thread_meta_store=threads)
    run_event_store = InMemoryRunEventStore()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=settings,
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        thread_meta_repo=threads,
    )
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(
            client=client,
            app=app,
            sys_admin_id=sys_admin_id,
            run_store=run_store,
            audit_store=audit_store,
        )


def _admin_headers(sys_admin_id: UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))}"
    }


@pytest.mark.asyncio
async def test_suspend_bulk_cancels_running_runs(ctx: _Ctx) -> None:
    target_tenant = uuid4()
    # A tenant_config row must exist for deactivate to flip its status.
    await ctx.app.state.tenant_config_repo.create(
        tenant_id=target_tenant, display_name="Acme", actor_id="seed"
    )
    # Seed a persistent RUNNING run in that tenant via the run manager.
    run_manager = ctx.app.state.agent_runtime.run_manager
    run_id = uuid4()
    await run_manager.create(run_id=run_id, thread_id=uuid4(), tenant_id=target_tenant)
    await run_manager.set_status(run_id, RunStatus.RUNNING)

    resp = await ctx.client.post(
        f"/v1/tenants/{target_tenant}/deactivate",
        headers=_admin_headers(ctx.sys_admin_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "suspended"

    # The in-flight run is terminated (previously suspend only rejected new runs).
    info = await ctx.run_store.get(run_id=run_id, tenant_id=target_tenant)
    assert info is not None
    assert info.status is RunStatus.INTERRUPTED

    page = await ctx.audit_store.query(AuditQuery(tenant_id=target_tenant, limit=1000))
    actions = [e.action for e in page.entries]
    assert AuditAction.TENANT_DEACTIVATE in actions
    assert AuditAction.SESSION_CANCEL in actions


@pytest.mark.asyncio
async def test_suspend_with_no_running_runs_still_succeeds(ctx: _Ctx) -> None:
    target_tenant = uuid4()
    await ctx.app.state.tenant_config_repo.create(
        tenant_id=target_tenant, display_name="Empty", actor_id="seed"
    )
    resp = await ctx.client.post(
        f"/v1/tenants/{target_tenant}/deactivate",
        headers=_admin_headers(ctx.sys_admin_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "suspended"


def _run_info(run_id: UUID, tenant: UUID, status: RunStatus, created_at: datetime) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=created_at,
        updated_at=created_at,
        finished_at=None,
    )


@pytest.mark.asyncio
async def test_bulk_cancel_paginates_and_includes_pending() -> None:
    """RT-4 — the helper enumerates RUNNING + PENDING across pages (past 500)."""
    store = InMemoryRunStore()
    tenant = uuid4()
    base = datetime(2026, 7, 4, tzinfo=UTC)
    # 600 RUNNING (spills to a second 500-row page) + 5 PENDING. Distinct
    # created_at makes offset pagination unambiguous.
    running_ids = [uuid4() for _ in range(600)]
    pending_ids = [uuid4() for _ in range(5)]
    for i, rid in enumerate(running_ids):
        await store.create(_run_info(rid, tenant, RunStatus.RUNNING, base + timedelta(seconds=i)))
    for i, rid in enumerate(pending_ids):
        await store.create(
            _run_info(rid, tenant, RunStatus.PENDING, base + timedelta(seconds=1000 + i))
        )
    # Runs are not in the RunManager registry, so cancel() returns False and the
    # store-level request_cancel fallback (running/pending → interrupted) fires.
    runtime = SimpleNamespace(run_manager=RunManager(store))
    audit = build_default_audit_logger(InMemoryAuditLogStore())

    cancelled = await _bulk_cancel_tenant_runs(
        tenant_id=tenant,
        run_store=store,
        runtime=runtime,  # type: ignore[arg-type]
        audit=audit,
        actor_id="admin",
        trace_id=None,
    )
    assert cancelled == 605  # all RUNNING + PENDING across both pages
    for rid in running_ids + pending_ids:
        row = await store.get(run_id=rid, tenant_id=tenant)
        assert row is not None
        assert row.status is RunStatus.INTERRUPTED


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_soon(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_bulk_cancel_broadcasts_run_cancel_for_peer_owned_runs_only() -> None:
    """跨副本取消亚秒化 —— 租户停用的批量取消:别的副本持有的 run 走 CAS,赢一个
    广播一个 ``run_cancel``;本副本自己的 run 就地叫停,不广播。"""
    store = InMemoryRunStore()
    tenant = uuid4()
    base = datetime(2026, 7, 4, tzinfo=UTC)
    peer_ids = [uuid4(), uuid4()]
    for i, rid in enumerate(peer_ids):
        await store.create(_run_info(rid, tenant, RunStatus.RUNNING, base + timedelta(seconds=i)))
    manager = RunManager(store)
    local = await manager.create(run_id=uuid4(), thread_id=uuid4(), tenant_id=tenant)
    assert await manager.set_status(local.run_id, RunStatus.RUNNING)
    spy = _SpyBus()

    cancelled = await _bulk_cancel_tenant_runs(
        tenant_id=tenant,
        run_store=store,
        runtime=SimpleNamespace(run_manager=manager),  # type: ignore[arg-type]
        audit=build_default_audit_logger(InMemoryAuditLogStore()),
        actor_id="admin",
        trace_id=None,
        bus=spy,
    )
    assert cancelled == 3
    assert local.abort_event.is_set()
    broadcast = sorted((e.kind, e.run_id, e.tenant_id) for e in spy.events)  # type: ignore[attr-defined]
    assert broadcast == sorted(("run_cancel", str(rid), str(tenant)) for rid in peer_ids)
