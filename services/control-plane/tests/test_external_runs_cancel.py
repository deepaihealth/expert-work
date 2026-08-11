"""External run-level cancel — ``POST /v1/agents/{agent_code}/runs/{run_id}:cancel``.

Covers: local abort (a run this replica's ``RunManager`` owns stops
immediately + flips to INTERRUPTED), cross-replica fallback (a run only the
durable store knows about — this replica's ``RunManager`` has no record — is
stopped via the CAS ``request_cancel``), ownership scoping (another user /
another agent both 404 as ``RUN_NOT_FOUND``, never leaking existence), and
idempotency (cancelling twice is a 200 with ``stopped: false`` the second
time, not an error).

Local vs. peer-owned runs are seeded directly against ``RunManager`` /
``RunStore`` rather than through ``POST /{agent_code}/runs``: that endpoint's
``mode="queue"`` only ever writes a ``QUEUED`` durable row (see
``RunManager.enqueue`` — "there is no in-memory record and no asyncio.Task");
nothing in this test process ever claims it (the distributed
``RunQueueWorker`` only starts from ``create_app``'s lifespan, which
``ASGITransport`` never triggers here), so a queue-mode run would stay
``QUEUED`` forever — a status neither ``RunManager.cancel`` nor
``RunStore.request_cancel`` transitions (both guard on RUNNING/PENDING only,
matching every other cancel call site in this codebase: ``agents.py``'s
agent-delete cascade and ``tenants.py``'s suspend bulk-cancel). ``mode="stream"``
would dodge that, but ``run_agent`` runs as a bare ``asyncio.create_task`` with
no artificial delay, so a real HTTP round trip races it to completion —
flaky. Direct seeding sidesteps both problems while still exercising the
endpoint's real ownership-check + two-level-cancel code paths. The 404 and
idempotency tests don't need a stoppable run at all (ownership fails before
either cancel primitive runs, and cancelling twice must not error either
way), so they use the real ``mode="queue"`` HTTP path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, Role
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
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

    async def bind_session(self, user_id: str) -> UUID:
        """Bind a session for ``user_id`` against ``support-bot`` and return its thread id."""
        bound = await self.client.post(
            "/v1/agents/support-bot/sessions", json={"user_id": user_id}, headers=self.headers
        )
        assert bound.status_code == 201, bound.text
        return UUID(bound.json()["data"]["session_id"])

    async def end_user_id(self, user_id: str) -> UUID:
        row = await self.app.state.tenant_user_repo.resolve(
            tenant_id=self.tenant_id, subject_type="user", subject_id=f"ext:{user_id}"
        )
        return row.id  # type: ignore[no-any-return]


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
async def test_cancel_stops_a_run_this_replica_owns(ctx: _Ctx) -> None:
    """A run registered in *this* process's ``RunManager`` (e.g. a live
    ``stream``-mode run) is aborted immediately via the local abort event, and
    the durable mirror flips to INTERRUPTED — the fast path of the two-level
    cancel described in the task brief."""
    await ctx.seed_agent()
    thread_id = await ctx.bind_session("cust-77")
    end_user_id = await ctx.end_user_id("cust-77")
    run_id = uuid4()
    await ctx.app.state.agent_runtime.run_manager.create(
        run_id=run_id, thread_id=thread_id, tenant_id=ctx.tenant_id, user_id=end_user_id
    )

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["run_id"] == str(run_id)
    assert body["data"]["stopped"] is True

    run = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_cancel_falls_back_to_request_cancel_for_a_peer_owned_run(ctx: _Ctx) -> None:
    """A run owned by another replica has no record in *this* process's
    ``RunManager`` — only the durable store knows about it. The endpoint must
    still stop it via the cross-replica ``request_cancel`` CAS.

    Regression coverage for the ``or await runs.request_cancel(...)``
    fallback: deleting it does not turn the "local run" test above red
    (this replica's manager already owns that run, so ``cancel`` alone
    returns ``True``), so without this test the fallback path ships with no
    protection at all.
    """
    await ctx.seed_agent()
    thread_id = await ctx.bind_session("cust-77")
    end_user_id = await ctx.end_user_id("cust-77")
    run_id = uuid4()
    # A second, independent RunManager sharing the same durable store —
    # standing in for a peer replica's registry. This process's real
    # run_manager (app.state.agent_runtime.run_manager) never sees this run.
    peer_manager = RunManager(store=ctx.run_store)
    await peer_manager.create(
        run_id=run_id, thread_id=thread_id, tenant_id=ctx.tenant_id, user_id=end_user_id
    )

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["stopped"] is True

    run = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_cancel_404s_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "someone-else"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_404s_for_another_agent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]

    resp = await ctx.client.post(
        f"/v1/agents/other-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cancel_is_idempotent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["run_id"]
    first = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert first.status_code == 200
    second = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    # A second cancel must not error — it reports that nothing was still running.
    assert second.status_code == 200, second.text
    assert second.json()["data"]["stopped"] is False
