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

import control_plane.api.agent_lifecycle as lifecycle
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
    name: claude-sonnet-4-6
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
    got_1 = await client.post("/v1/agents", json={"manifest_yaml": manifest})
    assert got_1.status_code == 201
    version = "2.0.0" if manifest is _YAML_V2 else "1.0.0"
    got_2 = await client.delete(f"/v1/agents/{_NAME}/{version}")
    assert got_2.status_code == 204


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
    got_3 = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML})
    assert got_3.status_code == 201
    disabled = await ctx.client.post(f"/v1/agents/{_NAME}/disable", json={"reason": "stop"})
    assert disabled.status_code == 200, disabled.text
    got_4 = await ctx.client.delete(f"/v1/agents/{_NAME}/1.0.0")
    assert got_4.status_code == 204

    got_5 = await _purge(ctx.client)
    assert got_5.status_code == 200  # type: ignore[attr-defined]
    got_6 = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML})
    assert got_6.status_code == 201

    detail = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert detail.json()["data"]["disabled"] is False


@pytest.mark.asyncio
async def test_purge_is_refused_while_any_version_is_live(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)
    v2 = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML_V2})
    assert v2.status_code == 201

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
    # Refused up front: nothing was touched (not even the purging marker).
    assert "summary" not in resp.json()["error"]["details"]  # type: ignore[attr-defined]
    detail = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert detail.json()["data"]["record"]["status"] == "deleted"


@pytest.mark.asyncio
async def test_confirm_name_must_match_exactly(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)

    resp = await _purge(ctx.client, confirm="Code-Reviewer")

    assert resp.status_code == 422  # type: ignore[attr-defined]
    assert resp.json()["error"]["code"] == "CONFIRM_NAME_MISMATCH"  # type: ignore[attr-defined]
    got_7 = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert got_7.status_code == 200


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

    got_8 = await _purge(ctx.client)
    assert got_8.status_code == 200  # type: ignore[attr-defined]

    got_9 = await usage.list_for_tenant(tenant_id=ctx.tenant_id, agent_name=_NAME)
    assert len(got_9) == 1
    page = await ctx.audit.query(AuditQuery(tenant_id=ctx.tenant_id, limit=1000))
    actions = {e.action for e in page.entries}
    assert {AuditAction.MANIFEST_WRITE, AuditAction.MANIFEST_DELETE} <= actions
    purged = [e for e in page.entries if e.action is AuditAction.MANIFEST_PURGE]
    assert len(purged) == 1 and purged[0].details["ok"] is True


@pytest.mark.asyncio
async def test_restore_keeps_triggers_off_and_says_how_many(ctx: _Ctx) -> None:
    got_10 = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML})
    assert got_10.status_code == 201
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
    got_11 = await ctx.client.delete(f"/v1/agents/{_NAME}/1.0.0")
    assert got_11.status_code == 204

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
        got_12 = await client.post(f"/v1/agents/{_NAME}/1.0.0/restore")
        assert got_12.status_code == status
        got_13 = await client.get(f"/v1/agents/{_NAME}/1.0.0/purge-preview")
        assert got_13.status_code == status
        got_14 = await _purge(client)
        assert got_14.status_code == status  # type: ignore[attr-defined]
    got_15 = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert got_15.status_code == 200
    got_16 = await ctx.client.post("/v1/agents/no-such/1.0.0/restore")
    assert got_16.status_code == 404


def test_the_purge_deps_are_wired_from_app_state() -> None:
    app = create_app(
        settings=Settings(
            env="dev", auth_mode="dev", oidc_issuer=TEST_ISSUER, oidc_audience=[TEST_AUDIENCE]
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    state = app.state
    deps = lifecycle.build_agent_purge_deps(SimpleNamespace(app=app))  # type: ignore[arg-type]

    assert deps.secret_allowlist is state.secret_allowlist_store
    assert deps.agent_disable is state.agent_disable_repo
    assert deps.memory_writeback_jobs is state.memory_writeback_job_store
    assert deps.quality_scores is state.quality_score_store
    assert deps.drift_alerts is state.quality_drift_alert_store
    assert deps.agent_file_versions is getattr(state, "agent_file_versions", None)
    assert deps.artifact_snapshots is getattr(state, "artifact_snapshots", None)


# --------------------------------------------------------------------------- B-278 review


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    async def publish(self, event: object) -> None:
        self.events.append((getattr(event, "kind", ""), getattr(event, "tenant_id", None)))


@pytest.mark.asyncio
async def test_a_purging_name_cannot_be_restored_or_recreated(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)
    repo = ctx.app.state.agent_spec_repo  # type: ignore[attr-defined]
    await repo.begin_purge(tenant_id=ctx.tenant_id, name=_NAME)

    restore = await ctx.client.post(f"/v1/agents/{_NAME}/1.0.0/restore")
    assert restore.status_code == 409
    assert restore.json()["success"] is False
    assert restore.json()["error"]["code"] == "AGENT_PURGING"
    assert restore.json()["error"]["details"] == {
        "name": _NAME,
        "version": "1.0.0",
        "status": "purging",
    }
    for manifest, version in ((_YAML, "1.0.0"), (_YAML_V2, "2.0.0")):
        create = await ctx.client.post("/v1/agents", json={"manifest_yaml": manifest})
        assert create.status_code == 409, create.text
        assert create.json()["error"]["code"] == "AGENT_PURGING"
        assert create.json()["error"]["details"] == {
            "name": _NAME,
            "version": version,
            "status": "purging",
        }
    detail = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert detail.json()["data"]["record"]["status"] == "purging"
    listed = await ctx.client.get("/v1/agents", params={"status": "deleted"})
    assert [r["name"] for r in listed.json()["data"]["items"]] == [_NAME]
    only_purging = await ctx.client.get("/v1/agents", params={"status": "purging"})
    assert only_purging.status_code == 200, only_purging.text
    assert [r["status"] for r in only_purging.json()["data"]["items"]] == ["purging"]
    only_active = await ctx.client.get("/v1/agents", params={"status": "active"})
    assert only_active.json()["data"]["items"] == []
    # A re-run of the purge is still allowed and finishes it.
    finish = await _purge(ctx.client)
    assert finish.status_code == 200  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_every_version_needs_delete_permission(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    await _create_and_delete(ctx.client)
    v2 = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML_V2})
    assert v2.status_code == 201
    deleted_v2 = await ctx.client.delete(f"/v1/agents/{_NAME}/2.0.0")
    assert deleted_v2.status_code == 204
    real = lifecycle.ensure_resource_access  # type: ignore[attr-defined]

    async def _not_someone_elses(
        request: object, *, resource: str, action: str, attrs: object
    ) -> None:
        if action == "delete" and getattr(attrs, "owner_id", None) == "someone-else":
            raise HTTPException(status_code=403, detail="not yours")
        await real(request, resource=resource, action=action, attrs=attrs)  # type: ignore[arg-type]

    repo = ctx.app.state.agent_spec_repo  # type: ignore[attr-defined]
    record = await repo.get(
        tenant_id=ctx.tenant_id, name=_NAME, version="2.0.0", include_deleted=True
    )
    repo._rows[(ctx.tenant_id, _NAME, "2.0.0")] = record.model_copy(
        update={"created_by": "someone-else"}
    )
    monkeypatch.setattr(lifecycle, "ensure_resource_access", _not_someone_elses)

    preview = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0/purge-preview")
    purge = await _purge(ctx.client)

    assert preview.status_code == 403
    assert purge.status_code == 403  # type: ignore[attr-defined]
    left = await repo.list_by_tenant(tenant_id=ctx.tenant_id, name=_NAME)
    assert {r.status.value for r in left} == {"deleted"}


@pytest.mark.asyncio
async def test_an_incomplete_purge_is_not_reported_as_success(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_and_delete(ctx.client)
    scores = ctx.app.state.quality_score_store  # type: ignore[attr-defined]
    calls = [0]
    original = scores.delete_for_agent

    async def _flaky(*, tenant_id: UUID, agent_name: str) -> int:
        calls[0] += 1
        if calls[0] == 1:
            raise OSError(5, "connect failed: postgresql://u:s3cret@db/x")
        return int(await original(tenant_id=tenant_id, agent_name=agent_name))

    monkeypatch.setattr(scores, "delete_for_agent", _flaky)

    first = await _purge(ctx.client)

    assert first.status_code == 409  # type: ignore[attr-defined]
    body = first.json()  # type: ignore[attr-defined]
    assert body["success"] is False
    assert body["error"]["code"] == "AGENT_PURGE_INCOMPLETE"
    assert body["error"]["details"]["failures"] == {"quality_score": "OSError"}
    assert "s3cret" not in first.text  # type: ignore[attr-defined]
    detail = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert detail.json()["data"]["record"]["status"] == "purging"
    second = await _purge(ctx.client)
    assert second.status_code == 200  # type: ignore[attr-defined]
    assert second.json()["data"]["ok"] is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_blocked_at_mark_or_at_final_delete_is_409(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from control_plane.purge.agent_purge import AgentPurgeBlockers

    async def _nothing_blocks(*_args: object, **_kwargs: object) -> AgentPurgeBlockers:
        return AgentPurgeBlockers(versions=[], active_versions=[], referenced_by=[])

    monkeypatch.setattr(lifecycle, "find_blockers", _nothing_blocks)
    await _create_and_delete(ctx.client)
    live = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML_V2})
    assert live.status_code == 201
    # A live version slipped past the pre-check: begin_purge refuses, nothing is deleted.
    at_mark = await _purge(ctx.client)
    assert at_mark.status_code == 409  # type: ignore[attr-defined]
    mark_error = at_mark.json()["error"]  # type: ignore[attr-defined]
    assert mark_error["details"]["active_versions"] == ["2.0.0"]
    detail = await ctx.client.get(f"/v1/agents/{_NAME}/1.0.0")
    assert detail.json()["data"]["record"]["status"] == "deleted"

    gone = await ctx.client.delete(f"/v1/agents/{_NAME}/2.0.0")
    assert gone.status_code == 204
    loader = ctx.app.state.manifest_loader  # type: ignore[attr-defined]
    parent = loader.load_from_string(
        _YAML.replace("name: code-reviewer", "name: parent").replace(
            "  tenant_config: {}",
            "  tenant_config: {}\n  subagents:\n"
            "    - name: reviewer\n"
            "      agent_ref: code-reviewer@1.0.0\n"
            "      description: delegate reviews",
        )
    )
    await ctx.app.state.agent_spec_repo.create(  # type: ignore[attr-defined]
        tenant_id=ctx.tenant_id, spec=parent, spec_sha256="a" * 64, created_by="u"
    )
    # A referrer slipped past the pre-check: the final transaction refuses.
    at_end = await _purge(ctx.client)
    assert at_end.status_code == 409  # type: ignore[attr-defined]
    error = at_end.json()["error"]  # type: ignore[attr-defined]
    assert error["code"] == "AGENT_PURGE_BLOCKED"
    assert error["details"]["referenced_by"] == ["parent@1.0.0"]
    assert error["details"]["summary"]["agent_spec_deleted"] is False


@pytest.mark.asyncio
async def test_clearing_the_default_agent_is_broadcast(ctx: _Ctx) -> None:
    from expert_work.protocol import TenantConfigPatch

    bus = _Bus()
    ctx.app.state.invalidation_bus = bus  # type: ignore[attr-defined]
    service = ctx.app.state.tenant_config_service  # type: ignore[attr-defined]
    await service.store.create(tenant_id=ctx.tenant_id, display_name="t", actor_id="u")
    await service.upsert(
        tenant_id=ctx.tenant_id, patch=TenantConfigPatch(default_agent_name=_NAME), actor_id="u"
    )
    await _create_and_delete(ctx.client)

    resp = await _purge(ctx.client)

    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert resp.json()["data"]["default_agent_cleared"] is True  # type: ignore[attr-defined]
    assert ("tenant_config", str(ctx.tenant_id)) in bus.events


@pytest.mark.asyncio
async def test_a_lock_conflict_is_409_busy(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    from expert_work.persistence.agent_spec import AgentSpecBusyError

    await _create_and_delete(ctx.client)
    repo = ctx.app.state.agent_spec_repo  # type: ignore[attr-defined]

    async def _deadlocked(*, tenant_id: UUID, name: str) -> list[str]:
        raise AgentSpecBusyError(tenant_id=tenant_id, name=name)

    monkeypatch.setattr(repo, "begin_purge", _deadlocked)
    purge = await _purge(ctx.client)
    assert purge.status_code == 409  # type: ignore[attr-defined]
    assert purge.json()["error"]["code"] == "AGENT_BUSY"  # type: ignore[attr-defined]

    async def _deadlocked_create(**_kwargs: object) -> object:
        raise AgentSpecBusyError(tenant_id=ctx.tenant_id, name=_NAME, version="2.0.0")

    monkeypatch.setattr(repo, "create", _deadlocked_create)
    create = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML_V2})
    assert create.status_code == 409, create.text
    assert create.json()["error"]["code"] == "AGENT_BUSY"
