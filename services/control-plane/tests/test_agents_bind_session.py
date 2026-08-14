"""External per-user session bind API — Stream Agent-Templates (M1-5b).

``POST /v1/agents/{agent_code}/sessions`` mints the end-user, resolves the agent
to its latest active version, creates/continues a conversation thread, records the
per-user instance binding + on_behalf_of audit. (The run itself is M1-5b-2.)
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
from expert_work.protocol import ALL_TENANTS, AgentSpec, AuditQuery
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


def _spec(*, name: str = "support-bot", version: str = "1.0.0") -> AgentSpec:
    doc = deepcopy(_SPEC)
    doc["metadata"]["name"] = name
    doc["metadata"]["version"] = version
    return AgentSpec.model_validate(doc)


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
        audit_store: InMemoryAuditLogStore,
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.audit_store = audit_store

    async def seed_agent(self, *, name: str = "support-bot", version: str = "1.0.0") -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id,
            spec=_spec(name=name, version=version),
            spec_sha256="a" * 64,
            created_by="seed",
        )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
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
        yield _Ctx(client, app, tenant_id, headers, audit_store)


@pytest.mark.asyncio
async def test_bind_mints_user_and_creates_session(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/sessions",
        json={"user_id": "app-cust-42"},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["agent_code"] == "support-bot"
    assert data["agent_version"] == "1.0.0"
    assert UUID(data["session_id"])  # a new thread
    assert UUID(data["user_id"])  # minted tenant_user.id


@pytest.mark.asyncio
async def test_same_external_user_remints_same_id(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "stable"}, headers=ctx.headers
    )
    second = await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "stable"}, headers=ctx.headers
    )
    # Mint-on-use is idempotent → same tenant_user.id; but each call w/o
    # session_id starts a NEW conversation thread.
    assert first.json()["data"]["user_id"] == second.json()["data"]["user_id"]
    assert first.json()["data"]["session_id"] != second.json()["data"]["session_id"]


@pytest.mark.asyncio
async def test_continue_existing_session(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "u"}, headers=ctx.headers
    )
    session_id = first.json()["data"]["session_id"]
    cont = await ctx.client.post(
        "/v1/agents/support-bot/sessions",
        json={"user_id": "u", "session_id": session_id},
        headers=ctx.headers,
    )
    assert cont.status_code == 201
    assert cont.json()["data"]["session_id"] == session_id


@pytest.mark.asyncio
async def test_unknown_agent_404(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        "/v1/agents/ghost/sessions", json={"user_id": "u"}, headers=ctx.headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_session_for_wrong_user_404(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "owner"}, headers=ctx.headers
    )
    session_id = first.json()["data"]["session_id"]
    # A different external user cannot continue owner's session.
    resp = await ctx.client.post(
        "/v1/agents/support-bot/sessions",
        json={"user_id": "intruder", "session_id": session_id},
        headers=ctx.headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_bind_emits_on_behalf_of_audit(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "u"}, headers=ctx.headers
    )
    entries = await ctx.audit_store.query(AuditQuery(tenant_id=ALL_TENANTS, limit=50))
    session_writes = [e for e in entries.entries if e.action == "session:write"]
    assert session_writes and session_writes[0].on_behalf_of is not None
