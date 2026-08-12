"""External on-behalf-of run API — Stream Agent-Templates (M1-5b-2).

``POST /v1/agents/{agent_code}/runs`` binds/continues a per-user session and spawns
the run **scoped to the minted end-user** (not the API-key caller). The key
assertion is that scoping: the run record's ``user_id`` is the end-user.
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
async def test_queue_run_scoped_to_minted_end_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["run_id"])

    # The minted end-user (mint-on-use). subject_id carries the `ext:` prefix
    # (see control_plane.api._external.EXTERNAL_SUBJECT_PREFIX) so an
    # app-supplied user_id can never collide with an employee's bare
    # Keycloak sub.
    end_user = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:cust-77"
    )
    # KEY: the run is scoped to the end-user, NOT the API-key caller.
    run = await ctx.run_store.get(run_id=run_id, tenant_id=ctx.tenant_id)
    assert run is not None
    assert run.user_id == end_user.id


@pytest.mark.asyncio
async def test_unknown_agent_404(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        "/v1/agents/ghost/runs",
        json={"user_id": "u", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_continue_session_reuses_thread(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    # Bind a session first.
    bound = await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "u"}, headers=ctx.headers
    )
    session_id = bound.json()["data"]["session_id"]
    # Run continuing that session → same thread_id in the 202 body.
    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "u", "session_id": session_id, "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202
    assert resp.json()["thread_id"] == session_id


@pytest.mark.asyncio
async def test_stream_run_sets_session_header(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    async with ctx.client.stream(
        "POST",
        "/v1/agents/support-bot/runs",
        json={"user_id": "u", "input": "hi"},
        headers=ctx.headers,
    ) as resp:
        assert resp.status_code == 200
        assert "X-Expert-Work-Session-Id" in resp.headers
        await resp.aclose()


def test_external_run_request_input_cap_matches_run_request() -> None:
    # ExternalRunRequest.input shares the same free-text cap as RunRequest.
    from pydantic import ValidationError

    from control_plane.api.agents import ExternalRunRequest
    from control_plane.api.runs import MAX_RUN_INPUT_CHARS

    ok = ExternalRunRequest(user_id="u", input="x" * MAX_RUN_INPUT_CHARS)
    assert ok.input is not None and len(ok.input) == MAX_RUN_INPUT_CHARS
    with pytest.raises(ValidationError):
        ExternalRunRequest(user_id="u", input="x" * (MAX_RUN_INPUT_CHARS + 1))
