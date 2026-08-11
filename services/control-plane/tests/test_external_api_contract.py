"""End-to-end contract walk for the external plane — the seams between tasks.

Each endpoint has its own unit tests; this walks the sequence a real
third-party app performs, so an id produced by one endpoint must be accepted by
the next without translation. External-API-v1 P1 Task 8.

Fixture mirrors ``test_external_uploads.py`` (object store + workspace store,
service-account/API-key principal) merged with ``test_external_sessions.py``
(run store / run event store wiring the run + cancel + events endpoints need).
"""

from __future__ import annotations

import base64
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
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools import RecordingWorkspaceStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
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
    def __init__(self, client: AsyncClient, app: Any, tenant_id: UUID, key_headers: dict[str, str]):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.key_headers = key_headers

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
    # ``stub_agent_runtime`` wires neither: the image-upload branch 503s
    # without an object store, the document branch 503s without a
    # workspace store (matches ``test_external_uploads.py``'s ctx fixture).
    app.state.object_store = InMemoryObjectStore()
    app.state.workspace_store = RecordingWorkspaceStore()

    tenant_id = uuid4()
    # A real third-party caller is a service-account (API-key) principal, not
    # a human JWT — ``write`` scope maps to OPERATOR, which grants both read
    # and write on the ``session`` resource (see ``auth/rbac.py``), so one
    # key drives every step of the chain, including the console-lockdown
    # check in step 7.
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-external-app",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    key_headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, key_headers)


@pytest.mark.asyncio
async def test_third_party_full_chain(ctx: _Ctx) -> None:
    await ctx.seed_agent()

    # 1. Upload a file first — no session exists yet, so the endpoint mints one.
    up = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.key_headers,
    )
    assert up.status_code == 201, up.text
    session_id = up.json()["data"]["session_id"]

    # 2. Run inside that same session.
    run = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "session_id": session_id,
            "input": "看下这张图",
            "mode": "queue",
        },
        headers=ctx.key_headers,
    )
    assert run.status_code == 202, run.text
    run_id = run.json()["run_id"]
    # The run must have landed in the SAME session the upload minted — proves
    # the upload's session_id needed no translation to be accepted by runs.
    assert run.json()["thread_id"] == session_id

    # 3. Cancel it.
    cancelled = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    # QUEUED is a cancellable state (Task 2) — a never-claimed queued run must
    # report a genuine stop, not the terminal-run "nothing to do" false.
    assert cancelled.json()["data"]["stopped"] is True

    # 4. The session shows up in that user's list — and only that user's.
    listed = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert listed.status_code == 200, listed.text
    ids = [s["session_id"] for s in listed.json()["data"]["sessions"]]
    assert session_id in ids

    # cust-99 must be a REAL registered user with its own session here — an
    # unrecognized user_id trivially returns [] (short-circuits before ever
    # touching the store; already covered by test_external_hardening.py's
    # test_list_sessions_never_mints_a_ghost_user) and would make this
    # assertion pass even if the store-level user_id scope were dropped
    # entirely. Binding cust-99 a session of its own is what actually
    # exercises the query-scoping code path.
    other_bound = await ctx.client.post(
        "/v1/agents/support-bot/sessions",
        json={"user_id": "cust-99"},
        headers=ctx.key_headers,
    )
    assert other_bound.status_code == 201, other_bound.text
    other_session_id = other_bound.json()["data"]["session_id"]

    other = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-99"},
        headers=ctx.key_headers,
    )
    other_ids = [s["session_id"] for s in other.json()["data"]["sessions"]]
    assert session_id not in other_ids
    assert other_ids == [other_session_id]

    # 5. Message history for that session is readable.
    msgs = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert msgs.status_code == 200, msgs.text

    # 6. Replaying the cancelled run's events works (reconnect path).
    events = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert events.status_code == 200, events.text
    assert "event: end" in events.text

    # 7. The whole chain ran on an API key that CANNOT touch the console plane.
    denied = await ctx.client.get("/v1/sessions", headers=ctx.key_headers)
    assert denied.status_code == 403, denied.text
