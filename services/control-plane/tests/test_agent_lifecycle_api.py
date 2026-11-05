"""B-278 —— ``/v1/agents/{name}/{version}/restore | purge-preview | purge`` over HTTP.

In-memory app (``persistence_backend`` default), real routers. Covers the
user-visible promises: purge then re-create the same name → 201; the kill
switch does not follow the name; purge is refused while a version is live or
another agent delegates to it; restore brings one version back without
re-enabling its triggers; usage and audit rows are kept; admin only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api.agent_lifecycle import build_agent_purge_deps
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.token_usage_store import TokenUsageRecord
from expert_work.protocol import AuditAction, AuditQuery, TriggerRecord
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_NAME = "code-reviewer"
_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""
_YAML_V2 = _YAML.replace('version: "1.0.0"', 'version: "2.0.0"')


@dataclass
class _Ctx:
    client: AsyncClient
    viewer: AsyncClient
    stranger: AsyncClient
    app: object
    tenant_id: UUID
    audit: InMemoryAuditLogStore


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    audit = InMemoryAuditLogStore()
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        audit_logger=build_default_audit_logger(audit),
        jwt_verifier=build_test_jwt_verifier(),
    )
    tenant_id = uuid4()
    transport = ASGITransport(app=app)
    base = "http://control-plane.test"

    def _headers(tenant: UUID, roles: tuple[str, ...]) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_test_jwt(tenant_id=tenant, roles=roles)}"}

    async with (
        AsyncClient(
            transport=transport, base_url=base, headers=_headers(tenant_id, ("admin",))
        ) as client,
        AsyncClient(
            transport=transport, base_url=base, headers=_headers(tenant_id, ("viewer",))
        ) as viewer,
        AsyncClient(
            transport=transport, base_url=base, headers=_headers(uuid4(), ("admin",))
        ) as stranger,
    ):
        yield _Ctx(
            client=client,
            viewer=viewer,
            stranger=stranger,
            app=app,
            tenant_id=tenant_id,
            audit=audit,
        )


async def _create_and_delete(client: AsyncClient, manifest: str = _YAML) -> None:
    assert (await client.post("/v1/agents", json={"manifest_yaml": manifest})).status_code == 201
    version = "2.0.0" if manifest is _YAML_V2 else "1.0.0"
    assert (await client.delete(f"/v1/agents/{_NAME}/{version}")).status_code == 204


async def _purge(client: AsyncClient, confirm: str = _NAME) -> object:
    return await client.post(f"/v1/agents/{_NAME}/1.0.0/purge", json={"confirm_name": confirm})


@pytest.mark.asyncio
async def test_purge_then_recreate_the_same_name_returns_201(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)

    resp = await _purge(ctx.client)

    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    data = resp.json()["data"]  # type: ignore[attr-defined]
    assert data["ok"] is True and data["agent_spec_deleted"] is True
    assert data["deleted"]["agent_spec"] == 1
    assert data["deleted"]["agent_spec_revision"] == 1
    again = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML})
    assert again.status_code == 201, again.text
    # The old history went with it: the new agent starts at revision 1 alone.
    revisions = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0/revisions")
    assert [r["revision"] for r in revisions.json()["data"]["items"]] == [1]


@pytest.mark.asyncio
async def test_the_kill_switch_does_not_survive_the_purge(ctx: _Ctx) -> None:
    assert (await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML})).status_code == 201
    disabled = await ctx.client.post(f"/v1/agents/{_NAME}/disable", json={"reason": "stop"})
    assert disabled.status_code == 200, disabled.text
    assert (await ctx.client.delete(f"/v1/agents/{_NAME}/1.0.0")).status_code == 204

    assert (await _purge(ctx.client)).status_code == 200  # type: ignore[attr-defined]
    assert (await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML})).status_code == 201

    detail = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert detail.json()["data"]["disabled"] is False


@pytest.mark.asyncio
async def test_purge_is_refused_while_any_version_is_live(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)
    assert (
        await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML_V2})
    ).status_code == 201

    resp = await _purge(ctx.client)

    assert resp.status_code == 409  # type: ignore[attr-defined]
    error = resp.json()["error"]  # type: ignore[attr-defined]
    assert error["code"] == "AGENT_PURGE_BLOCKED"
    assert error["details"] == {"active_versions": ["2.0.0"], "referenced_by": []}
    still = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert still.json()["data"]["record"]["status"] == "deleted"


@pytest.mark.asyncio
async def test_purge_is_refused_while_another_agent_delegates_to_it(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)
    loader = ctx.app.state.manifest_loader  # type: ignore[attr-defined]
    parent_yaml = _YAML.replace("name: code-reviewer", "name: parent").replace(
        "  tenant_config: {}",
        "  tenant_config: {}\n  subagents:\n"
        "    - name: reviewer\n"
        "      agent_ref: code-reviewer@1.0.0\n"
        "      description: delegate reviews",
    )
    spec = loader.load_from_string(parent_yaml)
    await ctx.app.state.agent_spec_repo.create(  # type: ignore[attr-defined]
        tenant_id=ctx.tenant_id, spec=spec, spec_sha256="a" * 64, created_by="u"
    )

    resp = await _purge(ctx.client)

    assert resp.status_code == 409  # type: ignore[attr-defined]
    assert resp.json()["error"]["details"]["referenced_by"] == ["parent@1.0.0"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_confirm_name_must_match_exactly(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)

    resp = await _purge(ctx.client, confirm="Code-Reviewer")

    assert resp.status_code == 422  # type: ignore[attr-defined]
    assert resp.json()["error"]["code"] == "CONFIRM_NAME_MISMATCH"  # type: ignore[attr-defined]
    assert (await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")).status_code == 200


@pytest.mark.asyncio
async def test_usage_and_audit_rows_are_kept(ctx: _Ctx) -> None:
    usage = ctx.app.state.token_usage_store  # type: ignore[attr-defined]
    await usage.insert(
        TokenUsageRecord(
            tenant_id=ctx.tenant_id,
            agent_name=_NAME,
            agent_version="1.0.0",
            model="m1",
            input_tokens=10,
            output_tokens=5,
        )
    )
    await _create_and_delete(ctx.client)

    assert (await _purge(ctx.client)).status_code == 200  # type: ignore[attr-defined]

    assert len(await usage.list_for_tenant(tenant_id=ctx.tenant_id, agent_name=_NAME)) == 1
    page = await ctx.audit.query(AuditQuery(tenant_id=ctx.tenant_id, limit=1000))
    actions = {e.action for e in page.entries}
    assert {AuditAction.MANIFEST_WRITE, AuditAction.MANIFEST_DELETE} <= actions
    purged = [e for e in page.entries if e.action is AuditAction.MANIFEST_PURGE]
    assert len(purged) == 1 and purged[0].details["ok"] is True


@pytest.mark.asyncio
async def test_restore_keeps_triggers_off_and_says_how_many(ctx: _Ctx) -> None:
    assert (await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML})).status_code == 201
    now = datetime.now(UTC)
    trigger = TriggerRecord(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        agent_name=_NAME,
        agent_version="1.0.0",
        name="nightly",
        kind="cron",
        config={"expr": "0 9 * * *"},
        enabled=True,
        source="api",
        created_at=now,
        updated_at=now,
    )
    triggers = ctx.app.state.trigger_store  # type: ignore[attr-defined]
    await triggers.create(trigger)
    assert (await ctx.client.delete(f"/v1/agents/{_NAME}/1.0.0")).status_code == 204

    resp = await ctx.client.post(f"/v1/agents/{_NAME}/1.0.0/restore")

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "name": _NAME,
        "version": "1.0.0",
        "status": "active",
        "triggers_disabled": 1,
    }
    detail = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert detail.json()["data"]["record"]["status"] == "active"
    after = await triggers.get(trigger_id=trigger.id, tenant_id=ctx.tenant_id)
    assert after is not None and after.enabled is False
    # Restoring a version that is not deleted is a caller error, not a no-op.
    again = await ctx.client.post(f"/v1/agents/{_NAME}/1.0.0/restore")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "AGENT_NOT_DELETED"


@pytest.mark.asyncio
async def test_preview_lists_counts_blockers_and_kept_tables(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)

    resp = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0/purge-preview")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == _NAME
    assert data["versions"] == ["1.0.0"]
    assert data["blocking"] == {"active_versions": [], "referenced_by": []}
    assert data["counts"]["revisions"] == 1
    assert data["counts"]["conversations"] == 0
    assert "token_usage" in data["kept"] and "tenant_billing_ledger" in data["kept"]
    assert data["default_agent_cleared"] is False


@pytest.mark.asyncio
async def test_only_admins_and_only_their_own_tenant(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)

    for client, status in ((ctx.viewer, 403), (ctx.stranger, 404)):
        assert (await client.post(f"/v1/agents/{_NAME}/1.0.0/restore")).status_code == status
        assert (await client.get(f"/v1/agents/{_NAME}/1.0.0/purge-preview")).status_code == status
        assert (await _purge(client)).status_code == status  # type: ignore[attr-defined]
    assert (await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")).status_code == 200
    assert (await ctx.client.post("/v1/agents/no-such/1.0.0/restore")).status_code == 404


def test_the_purge_deps_are_wired_from_app_state() -> None:
    app = create_app(
        settings=Settings(
            env="dev", auth_mode="dev", oidc_issuer=TEST_ISSUER, oidc_audience=[TEST_AUDIENCE]
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    state = app.state
    deps = build_agent_purge_deps(SimpleNamespace(app=app))  # type: ignore[arg-type]

    assert deps.secret_allowlist is state.secret_allowlist_store
    assert deps.agent_disable is state.agent_disable_repo
    assert deps.memory_writeback_jobs is state.memory_writeback_job_store
    assert deps.quality_scores is state.quality_score_store
    assert deps.drift_alerts is state.quality_drift_alert_store
    assert deps.agent_file_versions is getattr(state, "agent_file_versions", None)
    assert deps.artifact_snapshots is getattr(state, "artifact_snapshots", None)
