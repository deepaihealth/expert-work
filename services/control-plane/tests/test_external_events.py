"""External event replay / reconnect — ``GET /v1/agents/{agent_code}/runs/{run_id}/events``.

Covers: replaying a terminal run's durable event history, the ownership gate
404ing for another user's run BEFORE any ``text/event-stream`` body is ever
constructed, and ``user_id`` being a mandatory query parameter (422 when
missing, never a silent "list everything").
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunStatus,
    make_event_record,
)
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
        run_event_store: InMemoryRunEventStore,
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store
        self.run_event_store = run_event_store

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
    # External-API-v1 P2-b security fix (external_only()) — the external
    # plane is now service-account-only; this file's employee JWT was a
    # borrowed fixture (predates the gate), not a deliberate test of
    # console-JWT access.
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, run_store, run_event_store)


@pytest.mark.asyncio
async def test_events_replays_a_terminal_run(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["data"]["run_id"]
    # Seed durable frames BEFORE asserting anything — without real frames in
    # the store, "event: end" alone is also what the event_store=None
    # degenerate branch emits, so the test cannot tell "replayed real
    # history" apart from "no store wired, bail to a bare end frame".
    await ctx.run_event_store.append(
        make_event_record(run_id=UUID(run_id), seq=1, event_name="metadata", data={"step": 1})
    )
    await ctx.run_event_store.append(
        make_event_record(run_id=UUID(run_id), seq=2, event_name="updates", data={"step": 2})
    )
    # Drive the run to a terminal state so the endpoint takes the replay path.
    await ctx.run_store.set_status(
        run_id=UUID(run_id),
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["X-Expert-Work-Stream-Mode"] == "replay"
    assert "event: metadata" in resp.text
    assert "event: updates" in resp.text
    assert "event: end" in resp.text
    # SSE id is "{created_at_ms}-{seq}" — anchors the assertion to actual
    # replayed rows (seq 1 and 2), not just "some end frame showed up".
    assert re.search(r"id: \d+-1\n", resp.text)
    assert re.search(r"id: \d+-2\n", resp.text)


@pytest.mark.asyncio
async def test_events_404_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["data"]["run_id"]
    # Drive to terminal so a gate-bypass mutation resolves to a clean, fast
    # "replay returns 200" (asserted against below) instead of falling into
    # the live-attach path and hanging forever waiting for a bridge that will
    # never emit anything for a run nothing is driving.
    await ctx.run_store.set_status(
        run_id=UUID(run_id),
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "someone-else"},
        headers=ctx.headers,
    )
    # The gate must fire BEFORE the stream is built, so this is a plain HTTP
    # error — never an SSE frame carrying an error.
    assert resp.status_code == 404, resp.text
    assert not resp.headers["content-type"].startswith("text/event-stream")
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_events_requires_user_id(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["data"]["run_id"]
    resp = await ctx.client.get(f"/v1/agents/support-bot/runs/{run_id}/events", headers=ctx.headers)
    assert resp.status_code == 422, resp.text
