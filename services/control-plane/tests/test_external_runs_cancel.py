"""External run-level cancel — ``POST /v1/agents/{agent_code}/runs/{run_id}:cancel``.

Covers: local abort (a run this replica's ``RunManager`` owns stops
immediately + flips to INTERRUPTED), cross-replica fallback (a run only the
durable store knows about — this replica's ``RunManager`` has no record — is
stopped via the CAS ``request_cancel``), a still-queued run (never claimed by
any worker — the review-fix Critical C1 case, see below), ownership scoping
(another user / another agent both 404 as ``RUN_NOT_FOUND``, never leaking
existence), and idempotency (cancelling twice, or cancelling an already
finished run, is a 200 with ``stopped: false`` rather than erroring or lying).

Local vs. peer-owned runs are seeded directly against ``RunManager`` /
``RunStore`` rather than through ``POST /{agent_code}/runs``: ``mode="stream"``
would need the real endpoint, but ``run_agent`` runs as a bare
``asyncio.create_task`` with no artificial delay, so a real HTTP round trip
races it to completion before the test's own ``cancel`` call lands — flaky.
Direct seeding sidesteps that while still exercising the endpoint's real
ownership-check + two-level-cancel code paths.

Review fix (Critical C1): ``mode="queue"`` alone used to be a dead end for
this suite too — ``RunManager.enqueue`` writes only a durable ``QUEUED`` row
("there is no in-memory record and no asyncio.Task" — its own docstring), and
nothing in this test process ever claims it (the distributed
``RunQueueWorker`` only starts from ``create_app``'s lifespan, which
``ASGITransport`` never triggers here). Before the fix, neither
``RunManager.cancel`` nor ``RunStore.request_cancel`` transitioned a
``QUEUED`` row (both guarded on RUNNING/PENDING only), so a real third-party
caller cancelling a run still sitting in the queue got back ``200
{"stopped": false}`` — indistinguishable from "there was nothing to stop" —
and the run went on to execute anyway. Fixed at the ``RunStore.request_cancel``
CAS (now matches QUEUED too, in both the in-memory and SQL backends) rather
than in this endpoint, so ``test_cancel_stops_a_still_queued_run`` below
exercises the real HTTP ``mode="queue"`` path end-to-end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
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
    assert body["error"] is None

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
async def test_cancel_stops_a_still_queued_run(ctx: _Ctx) -> None:
    """Critical C1 review fix — a run started via the real ``mode="queue"``
    HTTP path and never claimed by any worker must be genuinely stoppable,
    not silently ignored while it goes on to execute.

    The ``claim_queued`` assertion is the one that actually proves the run
    can never run: a ``stopped: true`` response alone doesn't rule out a
    worker still winning the CAS race a moment later.
    """
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["data"]["run_id"]

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["stopped"] is True

    run = await ctx.run_store.get(run_id=UUID(run_id), tenant_id=ctx.tenant_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED

    # The load-bearing assertion: a worker racing to claim this run after the
    # cancel must find nothing — the run can never execute.
    now = datetime.now(UTC)
    claimed = await ctx.run_store.claim_queued(
        run_id=UUID(run_id),
        new_owner="late-worker",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
    )
    assert claimed is None


@pytest.mark.asyncio
async def test_cancel_404s_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = started.json()["data"]["run_id"]

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
    run_id = started.json()["data"]["run_id"]

    resp = await ctx.client.post(
        f"/v1/agents/other-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cancel_is_idempotent(ctx: _Ctx) -> None:
    """Cancelling a run that is genuinely stopped by the first call must be a
    true no-op on the second: ``RunManager.cancel`` returns ``True`` iff its
    in-memory record exists — regardless of status, and that record lingers
    ~5 minutes past completion — so without the endpoint's own status guard,
    a second cancel of an already-INTERRUPTED run would also lie and report
    ``stopped: true`` again. Uses a locally-owned run (not ``mode="queue"``)
    so the *first* call genuinely stops something — otherwise this test
    would never actually exercise "cancel something real, then cancel it
    again", and would pass identically even if every cancel were a no-op.
    """
    await ctx.seed_agent()
    thread_id = await ctx.bind_session("cust-77")
    end_user_id = await ctx.end_user_id("cust-77")
    run_id = uuid4()
    await ctx.app.state.agent_runtime.run_manager.create(
        run_id=run_id, thread_id=thread_id, tenant_id=ctx.tenant_id, user_id=end_user_id
    )

    first = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["stopped"] is True

    second = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    # A second cancel must not error — it reports that nothing was still running.
    assert second.status_code == 200, second.text
    assert second.json()["data"]["stopped"] is False


@pytest.mark.asyncio
async def test_cancel_is_a_noop_for_an_already_finished_run(ctx: _Ctx) -> None:
    """A run that already reached SUCCESS (not via cancel — e.g. it simply
    finished) must report ``stopped: false``, not ``true``.

    ``RunManager.cancel`` returns ``True`` iff a record exists in its
    registry, independent of status, so a finished run within the ~5-minute
    TTL sweep window would otherwise be reported as freshly stopped —
    exactly the false positive Important-I1 flagged.
    """
    await ctx.seed_agent()
    thread_id = await ctx.bind_session("cust-77")
    end_user_id = await ctx.end_user_id("cust-77")
    run_id = uuid4()
    await ctx.app.state.agent_runtime.run_manager.create(
        run_id=run_id, thread_id=thread_id, tenant_id=ctx.tenant_id, user_id=end_user_id
    )
    now = datetime.now(UTC)
    await ctx.run_store.set_status(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=now,
        finished_at=now,
    )

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["stopped"] is False

    run = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert run is not None
    assert run.status is RunStatus.SUCCESS
