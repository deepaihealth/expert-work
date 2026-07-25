"""Platform Agent template CRUD API — Stream Agent-Templates (M1-3).

Covers the system_admin CRUD surface, platform-scope gating (tenant admin → 403),
duplicate / not-found mappings, the extends-rejection guard, and audit emission.
Fixtures mirror test_mcp_catalog_api.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import ALL_TENANTS, AgentSpec, AgentSpecStatus, AuditQuery, Role
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_PREFIX = "/v1/platform/agent-templates"

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "platform-eng"},
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


def _spec(*, name: str = "support-bot", version: str = "1.0.0", **spec_over: Any) -> dict[str, Any]:
    doc = deepcopy(_SPEC)
    doc["metadata"]["name"] = name
    doc["metadata"]["version"] = version
    doc["spec"].update(spec_over)
    return doc


def _upsert(*, name: str = "support-bot", version: str = "1.0.0") -> dict[str, Any]:
    return {
        "spec": _spec(name=name, version=version),
        "display_name": "Support Bot",
        "description": "Customer support agent",
        "category": "support",
        "required_tier": "free",
        "status": "published",
    }


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
        audit_store: InMemoryAuditLogStore,
        admin_headers: dict[str, str],
        tenant_admin_headers: dict[str, str],
    ) -> None:
        self.client = client
        self.app = app
        self.audit_store = audit_store
        self.admin_headers = admin_headers
        self.tenant_admin_headers = tenant_admin_headers

    async def seed_tenant_spec(
        self,
        *,
        tenant_id: Any = None,
        name: str = "fork-bot",
        version: str = "1.0.0",
        extends: str | None = None,
    ) -> Any:
        """Seed one tenant agent_spec row (optionally declaring ``extends``)."""
        doc = _spec(name=name, version=version)
        if extends is not None:
            doc["spec"]["extends"] = extends
        return await self.app.state.agent_spec_repo.create(
            tenant_id=tenant_id or uuid4(),
            spec=AgentSpec.model_validate(doc),
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
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    admin_jwt = make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))
    admin_headers = {"Authorization": f"Bearer {admin_jwt}"}
    tenant_admin_jwt = make_test_jwt(tenant_id=uuid4(), subject=str(uuid4()), roles=("admin",))
    tenant_admin_headers = {"Authorization": f"Bearer {tenant_admin_jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, audit_store, admin_headers, tenant_admin_headers)


@pytest.mark.asyncio
async def test_create_get_list_patch_delete(ctx: _Ctx) -> None:
    create = await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["success"] is True
    assert body["data"]["name"] == "support-bot"
    assert body["data"]["version"] == "1.0.0"
    assert body["data"]["status"] == "published"

    got = await ctx.client.get(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert got.status_code == 200
    assert got.json()["data"]["display_name"] == "Support Bot"

    lst = await ctx.client.get(_PREFIX, headers=ctx.admin_headers)
    assert lst.status_code == 200
    assert {r["name"] for r in lst.json()["data"]} == {"support-bot"}

    patch = await ctx.client.patch(
        f"{_PREFIX}/support-bot/1.0.0", json={"status": "draft"}, headers=ctx.admin_headers
    )
    assert patch.status_code == 200
    assert patch.json()["data"]["status"] == "draft"
    # draft is filtered out of a published-only list.
    pub = await ctx.client.get(f"{_PREFIX}?status=published", headers=ctx.admin_headers)
    assert {r["name"] for r in pub.json()["data"]} == set()

    delete = await ctx.client.delete(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert delete.status_code == 204
    gone = await ctx.client.get(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_update_spec_round_trip(ctx: _Ctx) -> None:
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    new_spec = _spec(system_prompt={"template": "you are a fixed support agent"})
    put = await ctx.client.put(
        f"{_PREFIX}/support-bot/1.0.0", json=new_spec, headers=ctx.admin_headers
    )
    assert put.status_code == 200, put.text
    assert put.json()["data"]["spec"]["spec"]["system_prompt"]["template"] == (
        "you are a fixed support agent"
    )


@pytest.mark.asyncio
async def test_update_spec_identity_mismatch_422(ctx: _Ctx) -> None:
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    wrong = _spec(name="other-bot", version="1.0.0")
    put = await ctx.client.put(
        f"{_PREFIX}/support-bot/1.0.0", json=wrong, headers=ctx.admin_headers
    )
    assert put.status_code == 422
    assert put.json()["detail"]["code"] == "TEMPLATE_IDENTITY_MISMATCH"


@pytest.mark.asyncio
async def test_template_cannot_declare_extends(ctx: _Ctx) -> None:
    body = _upsert()
    body["spec"]["spec"]["extends"] = "another@1.0.0"
    resp = await ctx.client.post(_PREFIX, json=body, headers=ctx.admin_headers)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "TEMPLATE_CANNOT_EXTEND"


@pytest.mark.asyncio
async def test_duplicate_name_version_409(ctx: _Ctx) -> None:
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    dup = await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "TEMPLATE_DUPLICATE"


@pytest.mark.asyncio
async def test_tenant_admin_forbidden(ctx: _Ctx) -> None:
    resp = await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.tenant_admin_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_create_emits_audit(ctx: _Ctx) -> None:
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    entries = await ctx.audit_store.query(AuditQuery(tenant_id=ALL_TENANTS, limit=50))
    actions = {e.action for e in entries.entries}
    assert "agent_template:create" in actions


@pytest.mark.asyncio
async def test_get_missing_404(ctx: _Ctx) -> None:
    resp = await ctx.client.get(f"{_PREFIX}/ghost/9.9.9", headers=ctx.admin_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "TEMPLATE_NOT_FOUND"


# --- PR4 Task 2: delete pre-check — reverse lookup of extends dependents (D1) ---


async def _delete_audit_entries(ctx: _Ctx) -> list[Any]:
    entries = await ctx.audit_store.query(AuditQuery(tenant_id=ALL_TENANTS, limit=200))
    return [e for e in entries.entries if e.action == "agent_template:delete"]


@pytest.mark.asyncio
async def test_delete_with_pinned_dependent_409(ctx: _Ctx) -> None:
    """A tenant spec pinning ``extends=name@version`` blocks that version's delete."""
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    rec = await ctx.seed_tenant_spec(name="fork-bot", extends="support-bot@1.0.0")

    resp = await ctx.client.delete(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "TEMPLATE_IN_USE"
    assert detail["dependents_total"] == 1
    assert detail["dependents"] == [{"tenant_id": str(rec.tenant_id), "agent": "fork-bot@1.0.0"}]
    # The template survives the blocked delete (the inheritor keeps building).
    still = await ctx.client.get(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert still.status_code == 200
    # The 409 path emits no delete audit.
    assert await _delete_audit_entries(ctx) == []


@pytest.mark.asyncio
async def test_delete_without_dependents_204_and_audit_flag(ctx: _Ctx) -> None:
    """No inheritors → delete proceeds; audit details record the check ran."""
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    # A spec without extends and one extending a *different* template don't block.
    await ctx.seed_tenant_spec(name="plain-bot")
    await ctx.seed_tenant_spec(name="other-fork", extends="other-tpl@1.0.0")

    resp = await ctx.client.delete(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert resp.status_code == 204, resp.text
    deletes = await _delete_audit_entries(ctx)
    assert len(deletes) == 1
    assert deletes[0].details["dependents_checked"] is True


@pytest.mark.asyncio
async def test_delete_soft_deleted_dependent_does_not_block(ctx: _Ctx) -> None:
    """A soft-deleted (status=DELETED) inheritor is not a dependent."""
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    rec = await ctx.seed_tenant_spec(name="fork-bot", extends="support-bot@1.0.0")
    await ctx.app.state.agent_spec_repo.update_status(
        tenant_id=rec.tenant_id,
        name="fork-bot",
        version="1.0.0",
        status=AgentSpecStatus.DELETED,
    )

    resp = await ctx.client.delete(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_delete_latest_track_both_directions(ctx: _Ctx) -> None:
    """``extends=name@latest``: deleting a non-last version passes (latest still
    resolves — blocking would be a false positive); deleting the last resolvable
    version is blocked."""
    await ctx.client.post(_PREFIX, json=_upsert(version="1.0.0"), headers=ctx.admin_headers)
    await ctx.client.post(_PREFIX, json=_upsert(version="2.0.0"), headers=ctx.admin_headers)
    await ctx.seed_tenant_spec(name="fork-bot", extends="support-bot@latest")

    # Another PUBLISHED version remains → latest re-resolves → no block.
    first = await ctx.client.delete(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert first.status_code == 204, first.text

    # Now 2.0.0 is the last resolvable version → block.
    second = await ctx.client.delete(f"{_PREFIX}/support-bot/2.0.0", headers=ctx.admin_headers)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "TEMPLATE_IN_USE"


@pytest.mark.asyncio
async def test_delete_dependents_pagination_and_cap(ctx: _Ctx) -> None:
    """Dependents beyond the first cross-tenant page are still found (⑤) and the
    409 body caps ``dependents`` at 20 while ``dependents_total`` is exact (⑥)."""
    await ctx.client.post(_PREFIX, json=_upsert(), headers=ctx.admin_headers)
    # 21 dependents seeded first (oldest → sorted onto the later page)...
    for i in range(21):
        await ctx.seed_tenant_spec(name=f"fork-{i}", extends="support-bot@1.0.0")
    # ...then >200 non-extending fillers so the scan must paginate.
    for i in range(200):
        await ctx.seed_tenant_spec(name=f"filler-{i}")

    resp = await ctx.client.delete(f"{_PREFIX}/support-bot/1.0.0", headers=ctx.admin_headers)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["dependents_total"] == 21
    assert len(detail["dependents"]) == 20
