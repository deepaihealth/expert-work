"""External approval-decision endpoint — ``POST /v1/agents/{code}/runs/{id}:decide``.

Third-party API-key callers apply a human verdict on a paused run scoped to
their own end-user (``user_id``), exactly like the console's ``POST
/v1/sessions/{id}/runs/{id}/resume`` but gated through the external ownership
check (``_external.load_owned_run``) instead of session ownership.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, ApprovalRecord, ApprovalStatus, Role
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        headers: dict[str, str],
        run_store: InMemoryRunStore,
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    tenant_id = uuid4()
    jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=(Role.ADMIN.value,))
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, run_store)


@pytest.mark.asyncio
async def test_decide_404s_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "someone-else", "decision": "approve"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_decide_rejects_modified_args_without_modify(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "approve", "modified_args": {"x": 1}},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_decide_requires_modified_args_for_modify(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "modify"},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_decide_404s_when_no_pending_approval(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "approve"},
        headers=ctx.headers,
    )
    # Ownership passes; there is simply nothing waiting on a verdict.
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] != "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_decide_approve_applies_and_returns_new_run_id(ctx: _Ctx) -> None:
    """The invariant this endpoint exists for: a genuinely pending approval,
    once approved, is actually decided — not just answered with a plausible
    envelope. The stub orchestrator's fake LLM never emits a tool call (so it
    can never itself pause a run for approval); this seeds the pending
    ``agent_approval`` row directly, the way ``test_runs_api.py`` seeds one
    for the internal resume endpoint's own tests.
    """
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    body = started.json()
    run_id = UUID(body["run_id"])
    thread_id = UUID(body["thread_id"])

    end_user = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:cust-77"
    )

    # The stub orchestrator's fake LLM never emits a tool call on its own, so
    # nothing in this test harness can *organically* pause a run for
    # approval. Seed the graph's own checkpoint with a tool-calling AIMessage
    # so the resume path's routing (``state["messages"][-1]``) has something
    # real to inspect — otherwise ``resolve_approval_decision``'s
    # ``aupdate_state`` IndexErrors on an empty message list. ``get_agent``
    # cache-hits the same built graph the run-creation call above already
    # built (Stream MCP-OAUTH: keyed on (tenant, name, version) here, no
    # per-user OAuth pool configured in this fixture).
    built = await ctx.app.state.agent_runtime.get_agent(
        tenant_id=ctx.tenant_id,
        name="support-bot",
        version="1.0.0",
        spec=_spec(),
        user_id=str(end_user.id),
    )
    checkpoint_config: RunnableConfig = {
        "configurable": {"thread_id": str(thread_id), "tenant_id": str(ctx.tenant_id)}
    }
    await built.graph.aupdate_state(
        checkpoint_config,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "call-1", "name": "send_email", "args": {"to": "ops@example.com"}}
                    ],
                )
            ]
        },
        as_node="agent",
    )

    now = datetime.now(UTC)
    await ctx.app.state.approval_store.create(
        ApprovalRecord(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            user_id=end_user.id,
            run_id=run_id,
            thread_id=thread_id,
            request_id="approval:seed",
            node="tools",
            reason_kind="risk_confirmation",
            action_summary="approval-gated tool 'send_email'",
            proposed_args={"to": "ops@example.com"},
            requested_at=now,
            timeout_at=now + timedelta(hours=24),
        )
    )

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:decide",
        json={"user_id": "cust-77", "decision": "approve", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    new_run_id = UUID(data["run_id"])
    # Resume always continues under a NEW run_id — never the paused one.
    assert new_run_id != run_id
    assert resp.headers["X-Expert-Work-Run-Id"] == str(new_run_id)

    # The decision was actually persisted (not merely echoed back).
    decided = await ctx.app.state.approval_store.get_by_run(run_id=run_id, tenant_id=ctx.tenant_id)
    assert decided is not None
    assert decided.status is ApprovalStatus.APPROVED

    # And a continuation run really was minted for it.
    continuation = await ctx.run_store.get(run_id=new_run_id, tenant_id=ctx.tenant_id)
    assert continuation is not None
    assert continuation.is_resume is True
