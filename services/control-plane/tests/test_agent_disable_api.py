"""API tests for the agent kill switch — Stream RT-4 (RT-ADR-16/17/18).

Covers: disable sets the flag + AGENT_DISABLED audit; a disabled agent rejects
new runs at both the admission gate (``/v1/sessions/{id}/runs``) and the
external run gate (``/v1/agents/{code}/runs``); disable bulk-cancels an
in-flight run; a queued run for a disabled agent is not claimed by the
run-queue worker; enable clears the flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.run_queue_worker import RunQueueWorker
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
    RunStatus,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_AGENT_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: support-bot
  version: "1.0.0"
  tenant: acme
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you are support"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


class _Ctx:
    def __init__(
        self,
        *,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        run_store: InMemoryRunStore,
        threads: InMemoryThreadMetaStore,
        audit_store: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.run_store = run_store
        self.threads = threads
        self.audit_store = audit_store

    async def audit_actions(self) -> list[AuditAction]:
        page = await self.audit_store.query(AuditQuery(tenant_id=self.tenant_id, limit=1000))
        return [e.action for e in page.entries]

    def build_queue_worker(self) -> RunQueueWorker:
        return RunQueueWorker(
            run_store=self.app.state.run_store,
            thread_store=self.app.state.thread_meta_repo,
            agent_spec_store=self.app.state.agent_spec_repo,
            runtime=self.app.state.agent_runtime,
            audit_logger=self.app.state.audit_logger,
            approval_store=self.app.state.approval_store,
            agent_disable_service=self.app.state.agent_disable_service,
            tenant_status_service=self.app.state.tenant_status_service,
        )


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
    tenant_id = uuid4()
    jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=(Role.ADMIN.value,))
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=headers
    ) as client:
        # Register the agent through the real path.
        resp = await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        assert resp.status_code == 201, resp.text
        yield _Ctx(
            client=client,
            app=app,
            tenant_id=tenant_id,
            run_store=run_store,
            threads=threads,
            audit_store=audit_store,
        )


@pytest.mark.asyncio
async def test_disable_sets_flag_and_audits(ctx: _Ctx) -> None:
    resp = await ctx.client.post("/v1/agents/support-bot/disable", json={"reason": "incident-42"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["disabled"] is True

    record = await ctx.app.state.agent_disable_repo.get(
        tenant_id=ctx.tenant_id, agent_name="support-bot"
    )
    assert record is not None
    assert record.disabled is True
    assert record.reason == "incident-42"

    assert AuditAction.AGENT_DISABLED in await ctx.audit_actions()


@pytest.mark.asyncio
async def test_disable_unknown_agent_404(ctx: _Ctx) -> None:
    resp = await ctx.client.post("/v1/agents/no-such-agent/disable", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_enable_clears_flag(ctx: _Ctx) -> None:
    await ctx.client.post("/v1/agents/support-bot/disable", json={"reason": "x"})
    resp = await ctx.client.post("/v1/agents/support-bot/enable", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["disabled"] is False

    record = await ctx.app.state.agent_disable_repo.get(
        tenant_id=ctx.tenant_id, agent_name="support-bot"
    )
    assert record is not None
    assert record.disabled is False
    assert record.reason is None
    assert AuditAction.AGENT_ENABLED in await ctx.audit_actions()


@pytest.mark.asyncio
async def test_get_detail_surfaces_disabled_state(ctx: _Ctx) -> None:
    # RT-4 PR-2 — the agent detail response carries the kill-switch state so the
    # UI can render its status tag + disable/enable control without a 2nd call.
    before = await ctx.client.get("/v1/agents/support-bot/1.0.0")
    assert before.status_code == 200, before.text
    assert before.json()["data"]["disabled"] is False
    assert before.json()["data"]["disable"] is None

    await ctx.client.post("/v1/agents/support-bot/disable", json={"reason": "incident-42"})

    after = await ctx.client.get("/v1/agents/support-bot/1.0.0")
    data = after.json()["data"]
    assert data["disabled"] is True
    assert data["disable"]["disabled"] is True
    assert data["disable"]["reason"] == "incident-42"
    # The spec record is unchanged alongside the new fields.
    assert data["record"]["name"] == "support-bot"

    # Re-enabling drops the flag back off the detail payload.
    await ctx.client.post("/v1/agents/support-bot/enable", json={})
    reenabled = await ctx.client.get("/v1/agents/support-bot/1.0.0")
    assert reenabled.json()["data"]["disabled"] is False
    assert reenabled.json()["data"]["disable"] is None


@pytest.mark.asyncio
async def test_admission_gate_rejects_new_run_on_disabled_agent(ctx: _Ctx) -> None:
    # Bind a caller-owned session, then disable, then trigger a run on it.
    sess = await ctx.client.post(
        "/v1/sessions", json={"agent_name": "support-bot", "agent_version": "1.0.0"}
    )
    assert sess.status_code == 201, sess.text
    thread_id = sess.json()["data"]["thread_id"]

    await ctx.client.post("/v1/agents/support-bot/disable", json={})

    run = await ctx.client.post(f"/v1/sessions/{thread_id}/runs", json={"input": "hi"})
    assert run.status_code == 403, run.text
    assert run.json()["error"]["code"] == "AGENT_DISABLED"


@pytest.mark.asyncio
async def test_external_run_gate_rejects_disabled_agent(ctx: _Ctx) -> None:
    await ctx.client.post("/v1/agents/support-bot/disable", json={})
    # External-API-v1 P2-b security fix (external_only()) — the external run
    # endpoint is now service-account-only, unlike the rest of this file's
    # calls (``/v1/agents/support-bot/disable``, ``/v1/sessions``), which are
    # genuinely console-plane and must keep using ``ctx.client``'s employee
    # JWT. A dedicated service-account key for just this call, not a fixture
    # flip — flipping ``ctx`` would 403 this file's console calls instead.
    external_jwt = make_test_jwt(
        tenant_id=ctx.tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-1", "input": "hi", "mode": "queue"},
        headers={"Authorization": f"Bearer {external_jwt}"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "AGENT_DISABLED"


@pytest.mark.asyncio
async def test_disable_cancels_in_flight_run(ctx: _Ctx) -> None:
    # A real caller-owned session bound to the agent.
    sess = await ctx.client.post(
        "/v1/sessions", json={"agent_name": "support-bot", "agent_version": "1.0.0"}
    )
    thread_id = UUID(sess.json()["data"]["thread_id"])

    # Seed a persistent RUNNING run for that thread via the run manager.
    run_manager = ctx.app.state.agent_runtime.run_manager
    run_id = uuid4()
    await run_manager.create(run_id=run_id, thread_id=thread_id, tenant_id=ctx.tenant_id)
    await run_manager.set_status(run_id, RunStatus.RUNNING)

    resp = await ctx.client.post("/v1/agents/support-bot/disable", json={"reason": "stop"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["cancelled_runs"] == 1

    info = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert info is not None
    assert info.status is RunStatus.INTERRUPTED

    actions = await ctx.audit_actions()
    assert AuditAction.SESSION_CANCEL in actions
    assert AuditAction.AGENT_DISABLED in actions


@pytest.mark.asyncio
async def test_disable_cancels_a_run_owned_by_another_replica(ctx: _Ctx) -> None:
    # RT-ADR-17 cross-replica — a RUNNING run this instance's RunManager does NOT
    # own (seeded straight into the store, as if it is executing on a peer replica)
    # must still be stopped: RunManager.cancel returns False for it, so the loop
    # falls back to the guarded store request_cancel, which flips it to INTERRUPTED
    # (the peer's next lease-heartbeat CAS then fails and it aborts).
    sess = await ctx.client.post(
        "/v1/sessions", json={"agent_name": "support-bot", "agent_version": "1.0.0"}
    )
    thread_id = UUID(sess.json()["data"]["thread_id"])

    run_id = uuid4()
    now = datetime.now(UTC)
    await ctx.run_store.create(
        RunInfo(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            thread_id=thread_id,
            user_id=None,
            status=RunStatus.RUNNING,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
            claimed_by="peer-instance-xyz",
        )
    )

    resp = await ctx.client.post("/v1/agents/support-bot/disable", json={"reason": "stop"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["cancelled_runs"] == 1

    info = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert info is not None
    assert info.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_queued_run_for_disabled_agent_not_claimed(ctx: _Ctx) -> None:
    sess = await ctx.client.post(
        "/v1/sessions", json={"agent_name": "support-bot", "agent_version": "1.0.0"}
    )
    thread_id = UUID(sess.json()["data"]["thread_id"])

    # Enqueue a run for that thread (durable QUEUED, owned by no process yet).
    run_manager = ctx.app.state.agent_runtime.run_manager
    run_id = uuid4()
    await run_manager.enqueue(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=ctx.tenant_id,
        enqueued_input={"input": "hi"},
    )

    await ctx.client.post("/v1/agents/support-bot/disable", json={})

    worker = ctx.build_queue_worker()
    started = await worker.run_once()
    assert started == 0

    info = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert info is not None
    assert info.status is RunStatus.QUEUED  # left queued, not executed


@pytest.mark.asyncio
async def test_reenabled_agent_queued_run_is_claimed(ctx: _Ctx) -> None:
    sess = await ctx.client.post(
        "/v1/sessions", json={"agent_name": "support-bot", "agent_version": "1.0.0"}
    )
    thread_id = UUID(sess.json()["data"]["thread_id"])

    run_manager = ctx.app.state.agent_runtime.run_manager
    run_id = uuid4()
    await run_manager.enqueue(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=ctx.tenant_id,
        enqueued_input={"input": "hi"},
    )

    await ctx.client.post("/v1/agents/support-bot/disable", json={})
    await ctx.client.post("/v1/agents/support-bot/enable", json={})

    worker = ctx.build_queue_worker()
    started = await worker.run_once()
    assert started == 1  # re-enabled → the queued run is claimed + executed
