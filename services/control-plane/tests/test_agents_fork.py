"""Tenant fork-from-template API — Stream Agent-Templates (M1-4).

``POST /v1/agents/fork`` materializes a published platform template into a
tenant-owned agent_spec with ``extends`` pinned to the resolved version. Covers
success + version pinning, @latest resolution, tier entitlement, not-found,
duplicate.
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
from expert_work.protocol import (
    AgentSpec,
    PlatformAgentTemplateStatus,
    PlatformAgentTemplateUpsert,
    Role,
    TenantPlan,
)
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "defenses": {"output_screen": "block"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _template_spec(*, name: str = "support-bot", version: str = "1.0.0") -> AgentSpec:
    doc = deepcopy(_SPEC)
    doc["metadata"]["name"] = name
    doc["metadata"]["version"] = version
    return AgentSpec.model_validate(doc)


def _upsert(
    *,
    name: str = "support-bot",
    version: str = "1.0.0",
    status: PlatformAgentTemplateStatus = PlatformAgentTemplateStatus.PUBLISHED,
    required_tier: TenantPlan = TenantPlan.FREE,
) -> PlatformAgentTemplateUpsert:
    return PlatformAgentTemplateUpsert(
        spec=_template_spec(name=name, version=version),
        display_name="Support Bot",
        category="support",
        status=status,
        required_tier=required_tier,
    )


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
    def __init__(self, client: AsyncClient, app: Any, tenant_id: UUID, headers: dict[str, str]):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers

    async def seed_template(self, upsert: PlatformAgentTemplateUpsert) -> None:
        await self.app.state.platform_agent_template_store.create(
            upsert=upsert, created_by="sysadmin"
        )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
    )
    tenant_id = uuid4()
    jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=(Role.ADMIN.value,))
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers)


@pytest.mark.asyncio
async def test_fork_pins_extends_and_renames(ctx: _Ctx) -> None:
    await ctx.seed_template(_upsert(version="1.0.0"))
    resp = await ctx.client.post(
        "/v1/agents/fork",
        json={"template_name": "support-bot", "template_version": "1.0.0", "name": "alice-support"},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    record = resp.json()["data"]["record"]
    assert record["name"] == "alice-support"
    # extends pinned to the concrete version; floor re-applies at build.
    assert record["spec"]["spec"]["extends"] == "support-bot@1.0.0"
    assert record["spec"]["metadata"]["tenant"] == str(ctx.tenant_id)
    # tier③ copied from base (tenant edits it later via normal CRUD).
    assert record["spec"]["spec"]["system_prompt"]["template"] == "you are support"


@pytest.mark.asyncio
async def test_fork_latest_resolves_and_pins_newest(ctx: _Ctx) -> None:
    await ctx.seed_template(_upsert(version="1.0.0"))
    await ctx.seed_template(_upsert(version="2.0.0"))
    resp = await ctx.client.post(
        "/v1/agents/fork",
        json={"template_name": "support-bot", "template_version": "latest", "name": "bob-support"},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    # @latest resolved to the newest published version, then PINNED.
    assert resp.json()["data"]["record"]["spec"]["spec"]["extends"] == "support-bot@2.0.0"


@pytest.mark.asyncio
async def test_fork_tier_forbidden(ctx: _Ctx) -> None:
    await ctx.seed_template(_upsert(required_tier=TenantPlan.ENTERPRISE))
    resp = await ctx.client.post(
        "/v1/agents/fork",
        json={"template_name": "support-bot", "name": "x"},
        headers=ctx.headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TEMPLATE_TIER_FORBIDDEN"


@pytest.mark.asyncio
async def test_fork_viewer_role_is_forbidden(ctx: _Ctx) -> None:
    """Backlog Task 2 (spec/external-api-v1-p2b) — a VIEWER-role employee
    must not be able to fork a template into a new tenant-owned agent
    (creating a tenant asset). This is enforced by step 4's
    ``ensure_resource_access(manifest, write, ...)`` call (parity with
    ``create_agent``), not by ``console_only()`` — that gate only rejects
    service-account principals, and an employee JWT is a ``user`` principal.
    VIEWER's grants for ``manifest`` are read-only (``rbac.py``), and ABAC
    conditioned bindings only narrow a role's own grants, never widen them,
    so no binding shape can let a VIEWER through here.
    """
    await ctx.seed_template(_upsert())
    viewer_jwt = make_test_jwt(
        tenant_id=ctx.tenant_id, subject=str(uuid4()), roles=(Role.VIEWER.value,)
    )
    viewer_headers = {"Authorization": f"Bearer {viewer_jwt}"}
    resp = await ctx.client.post(
        "/v1/agents/fork",
        json={"template_name": "support-bot", "template_version": "1.0.0", "name": "viewer-x"},
        headers=viewer_headers,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_fork_draft_template_not_available(ctx: _Ctx) -> None:
    await ctx.seed_template(_upsert(status=PlatformAgentTemplateStatus.DRAFT))
    # @latest filters to PUBLISHED, so a draft-only template resolves to nothing.
    resp = await ctx.client.post(
        "/v1/agents/fork",
        json={"template_name": "support-bot", "name": "x"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TEMPLATE_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_fork_missing_template_404(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        "/v1/agents/fork",
        json={"template_name": "ghost", "template_version": "1.0.0", "name": "x"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TEMPLATE_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_fork_duplicate_name_409(ctx: _Ctx) -> None:
    await ctx.seed_template(_upsert())
    body = {"template_name": "support-bot", "template_version": "1.0.0", "name": "dup"}
    first = await ctx.client.post("/v1/agents/fork", json=body, headers=ctx.headers)
    assert first.status_code == 201
    second = await ctx.client.post("/v1/agents/fork", json=body, headers=ctx.headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "MANIFEST_DUPLICATE"


@pytest.mark.asyncio
async def test_list_templates_dedups_to_latest_published(ctx: _Ctx) -> None:
    await ctx.seed_template(_upsert(version="1.0.0"))
    await ctx.seed_template(_upsert(version="2.0.0"))
    # A draft-only template must not surface in the marketplace browse.
    await ctx.seed_template(_upsert(name="beta-bot", status=PlatformAgentTemplateStatus.DRAFT))
    resp = await ctx.client.get("/v1/agents/templates", headers=ctx.headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]
    # One card per name (latest published version), drafts excluded.
    assert [it["name"] for it in items] == ["support-bot"]
    card = items[0]
    assert card["version"] == "2.0.0"
    assert card["display_name"] == "Support Bot"
    assert card["can_fork"] is True  # FREE template, FREE tenant
    assert "spec" not in card  # browse payload omits the base manifest


@pytest.mark.asyncio
async def test_list_templates_marks_unaffordable_tier(ctx: _Ctx) -> None:
    await ctx.seed_template(_upsert(required_tier=TenantPlan.ENTERPRISE))
    resp = await ctx.client.get("/v1/agents/templates", headers=ctx.headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["can_fork"] is False
