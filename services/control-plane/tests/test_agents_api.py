"""End-to-end tests for ``/v1/agents`` CRUD."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import TriggerStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.protocol import AuditAction, AuditQuery, Role, TriggerRecord
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore, RunStatus
from orchestrator import AgentFactoryError
from orchestrator.errors import SkillNotFoundError
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)

_DEFAULT_TENANT = DEFAULT_DEV_TENANT_ID

_VALID_YAML = """\
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

_JINJA_YAML = _VALID_YAML.replace(
    'template: "you are a reviewer"',
    'template: "you are {{ persona }}"\n'
    "    jinja: true\n"
    "    variables:\n"
    "      - name: persona\n"
    "        required: true",
)


async def _put_manifest(client, name: str, version: str, manifest_yaml: str):
    """PUT 一版 manifest,带上当前的 ``If-Match``。

    更新是必须带前置条件的(并发编辑保护),而这些用例验的不是并发 —— 所以
    先读一次当前 sha 再写,正是控制台自己的做法。
    """
    current = await client.get(f"/v1/agents/{name}/{version}")
    sha = current.json()["data"]["record"]["spec_sha256"]
    return await client.put(
        f"/v1/agents/{name}/{version}",
        json={"manifest_yaml": manifest_yaml},
        headers={"If-Match": sha},
    )


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def b5_client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
    """A control-plane client that uses an InMemoryAuditLogStore the test
    can introspect (the default fixture builds an isolated audit logger)."""
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    audit_logger = build_default_audit_logger(audit_store)
    app = create_app(
        settings=settings,
        audit_logger=audit_logger,
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=headers,
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_creates_agent_and_emits_audit(
    b5_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    response = await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    record = body["data"]["record"]
    assert record["name"] == "code-reviewer"
    assert record["version"] == "1.0.0"
    assert record["status"] == "active"
    assert len(record["spec_sha256"]) == 64

    # Audit row landed.
    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    assert any(
        r.action.value == "manifest:write" and r.result.value == "success" for r in page.entries
    )


@pytest.mark.asyncio
async def test_duplicate_returns_409(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    response = await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "MANIFEST_DUPLICATE"


@pytest.mark.asyncio
async def test_invalid_manifest_returns_422_with_errors(b5_client: AsyncClient) -> None:
    broken = _VALID_YAML.replace("kind: Agent\n", "")
    response = await b5_client.post("/v1/agents", json={"manifest_yaml": broken})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "MANIFEST_INVALID"
    assert body["error"]["errors"]


@pytest.mark.asyncio
async def test_yaml_syntax_error_returns_400(b5_client: AsyncClient) -> None:
    response = await b5_client.post("/v1/agents", json={"manifest_yaml": "this: is: broken"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MANIFEST_SYNTAX"


@pytest.mark.asyncio
async def test_post_keeps_jinja_braces_verbatim(b5_client: AsyncClient) -> None:
    """Jinja 动态 prompt 的 {{ }} 属于 run 期(prompt_render),保存时必须原样入库。
    控制台保存带 {{ }} 的 prompt 曾一律 400 MANIFEST_TEMPLATE(调试台重设计 PR0 Bug B)。"""
    response = await b5_client.post("/v1/agents", json={"manifest_yaml": _JINJA_YAML})
    assert response.status_code == 201, response.text
    detail = await b5_client.get("/v1/agents/code-reviewer/1.0.0")
    assert detail.status_code == 200
    prompt = detail.json()["data"]["record"]["spec"]["spec"]["system_prompt"]
    assert prompt["template"] == "you are {{ persona }}"
    assert prompt["jinja"] is True
    assert [v["name"] for v in prompt["variables"]] == ["persona"]


@pytest.mark.asyncio
async def test_post_rejects_removed_template_vars_field(b5_client: AsyncClient) -> None:
    """``template_vars`` 已下线;ManifestPayload 是 extra=forbid,带它的请求 422。"""
    response = await b5_client.post(
        "/v1/agents",
        json={"manifest_yaml": _VALID_YAML, "template_vars": {"name": "x"}},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# read / list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_single_agent(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    response = await b5_client.get("/v1/agents/code-reviewer/1.0.0")
    assert response.status_code == 200
    record = response.json()["data"]["record"]
    assert record["name"] == "code-reviewer"


@pytest.mark.asyncio
async def test_get_returns_404_when_missing(b5_client: AsyncClient) -> None:
    response = await b5_client.get("/v1/agents/no-such/9.9.9")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_after_two_posts(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    second = _VALID_YAML.replace('version: "1.0.0"', 'version: "1.0.1"')
    await b5_client.post("/v1/agents", json={"manifest_yaml": second})
    response = await b5_client.get("/v1/agents?name=code-reviewer")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_list_filters_by_status(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    response = await b5_client.get("/v1/agents?status=deleted")
    assert response.json()["data"]["total"] == 0


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_replaces_spec(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    updated_yaml = _VALID_YAML.replace(
        'template: "you are a reviewer"',
        'template: "you are a senior reviewer"',
    )
    response = await _put_manifest(b5_client, "code-reviewer", "1.0.0", updated_yaml)
    assert response.status_code == 200
    spec = response.json()["data"]["record"]["spec"]["spec"]["system_prompt"]["template"]
    assert spec == "you are a senior reviewer"


@pytest.mark.asyncio
async def test_put_path_mismatch_returns_422(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    response = await b5_client.put(
        "/v1/agents/different-name/1.0.0",
        json={"manifest_yaml": _VALID_YAML},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_PATH_MISMATCH"


@pytest.mark.asyncio
async def test_put_404_when_missing(b5_client: AsyncClient) -> None:
    response = await b5_client.put(
        "/v1/agents/code-reviewer/1.0.0",
        json={"manifest_yaml": _VALID_YAML},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_soft_removes(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    response = await b5_client.delete("/v1/agents/code-reviewer/1.0.0")
    assert response.status_code == 204

    # GET no longer returns the row (soft-deleted rows are hidden).
    follow_up = await b5_client.get("/v1/agents/code-reviewer/1.0.0")
    assert follow_up.status_code == 404


@pytest.mark.asyncio
async def test_delete_404_when_missing(b5_client: AsyncClient) -> None:
    response = await b5_client.delete("/v1/agents/no-such/9.9.9")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_tenant_cannot_see_agent(b5_client: AsyncClient) -> None:
    from uuid import UUID

    # Default ``b5_client`` JWT is tied to ``_DEFAULT_TENANT``.
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})

    other_tenant = UUID("11111111-1111-1111-1111-111111111111")
    other_jwt = make_test_jwt(tenant_id=other_tenant)
    response = await b5_client.get(
        "/v1/agents/code-reviewer/1.0.0",
        headers={"Authorization": f"Bearer {other_jwt}"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Stream HX-5 — revision history / rollback
# ---------------------------------------------------------------------------

_UPDATED_YAML = _VALID_YAML.replace("you are a reviewer", "you are a strict reviewer")


@pytest.mark.asyncio
async def test_revisions_list_and_get_snapshot(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    await _put_manifest(b5_client, "code-reviewer", "1.0.0", _UPDATED_YAML)

    listing = await b5_client.get("/v1/agents/code-reviewer/1.0.0/revisions")
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    assert [i["revision"] for i in items] == [2, 1]
    assert items[0]["actor_id"]
    assert len(items[0]["spec_sha256"]) == 64
    assert "spec" not in items[0]  # summaries only — diff fetches snapshots

    snap = await b5_client.get("/v1/agents/code-reviewer/1.0.0/revisions/1")
    assert snap.status_code == 200
    record = snap.json()["data"]["record"]
    assert record["revision"] == 1
    assert record["spec"]["spec"]["system_prompt"]["template"] == "you are a reviewer"

    missing = await b5_client.get("/v1/agents/code-reviewer/1.0.0/revisions/9")
    assert missing.status_code == 404
    unknown_agent = await b5_client.get("/v1/agents/nope/1.0.0/revisions")
    assert unknown_agent.status_code == 404


@pytest.mark.asyncio
async def test_rollback_appends_revision_with_old_content(
    b5_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    await _put_manifest(b5_client, "code-reviewer", "1.0.0", _UPDATED_YAML)

    response = await b5_client.post("/v1/agents/code-reviewer/1.0.0/revisions/1/rollback")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["rolled_back_to"] == 1
    assert data["revision"] == 3  # rollback moved *forward* to old content
    assert data["record"]["spec"]["spec"]["system_prompt"]["template"] == "you are a reviewer"

    # History now has three entries; current content equals revision 1's.
    listing = await b5_client.get("/v1/agents/code-reviewer/1.0.0/revisions")
    items = listing.json()["data"]["items"]
    assert [i["revision"] for i in items] == [3, 2, 1]
    assert items[0]["spec_sha256"] == items[2]["spec_sha256"]

    # Audit row carries the rollback provenance.
    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    rollbacks = [
        r
        for r in page.entries
        if r.action.value == "manifest:write" and (r.details or {}).get("rolled_back_to") == 1
    ]
    assert len(rollbacks) == 1
    assert rollbacks[0].details["revision"] == 3


@pytest.mark.asyncio
async def test_rollback_to_current_content_is_recorded_noop(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})

    response = await b5_client.post("/v1/agents/code-reviewer/1.0.0/revisions/1/rollback")
    assert response.status_code == 200
    assert response.json()["data"]["revision"] is None  # same sha — nothing recorded

    listing = await b5_client.get("/v1/agents/code-reviewer/1.0.0/revisions")
    assert [i["revision"] for i in listing.json()["data"]["items"]] == [1]


@pytest.mark.asyncio
async def test_rollback_unknown_revision_404(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    response = await b5_client.post("/v1/agents/code-reviewer/1.0.0/revisions/7/rollback")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rollback_needs_manifest_write_while_the_reads_stay_open(
    b5_client: AsyncClient,
) -> None:
    """User ruling 2026-08-12 — of the five console routes the P1 lockdown
    closed to API keys, ``rollback`` is the only **write**, and it carried no
    employee-side authorization at all: any logged-in VIEWER could roll a
    tenant's manifest back to an arbitrary older snapshot. It now needs
    ``manifest:write``. The reads in the same group deliberately stay open to
    every employee — blocking them was explicitly rejected, so this asserts
    both halves; a bare ``console_only()`` on all five would satisfy only one.
    """
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    await _put_manifest(b5_client, "code-reviewer", "1.0.0", _UPDATED_YAML)

    viewer = {
        "Authorization": "Bearer "
        + make_test_jwt(
            tenant_id=_DEFAULT_TENANT, subject="viewer-user", roles=(Role.VIEWER.value,)
        )
    }

    denied = await b5_client.post(
        "/v1/agents/code-reviewer/1.0.0/revisions/1/rollback", headers=viewer
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["message"] == "principal lacks required role"

    # The rollback really did not happen — a 403 that still writes would be
    # worse than no gate at all.
    listing = await b5_client.get("/v1/agents/code-reviewer/1.0.0/revisions")
    assert [i["revision"] for i in listing.json()["data"]["items"]] == [2, 1]

    # Same VIEWER, the reads that stay open.
    for path in (
        "/v1/agents",
        "/v1/agents/code-reviewer/1.0.0/revisions",
        "/v1/agents/code-reviewer/1.0.0/revisions/1",
        "/v1/agents/code-reviewer/1.0.0/users",
    ):
        readable = await b5_client.get(path, headers=viewer)
        assert readable.status_code == 200, f"{path} -> {readable.status_code} {readable.text}"

    # And a role that does hold ``manifest:write`` still rolls back.
    allowed = await b5_client.post("/v1/agents/code-reviewer/1.0.0/revisions/1/rollback")
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["data"]["rolled_back_to"] == 1


# ---------------------------------------------------------------------------
# runtime build-cache invalidation on write — an in-place config edit (approval
# gate, tools, model, prompt) must take effect on the next run WITHOUT a
# control-plane restart. ``AgentRuntime`` caches built agents by
# ``(tenant, name, version)`` and only consults the spec on a miss, so a
# same-version spec change is invisible unless the write path invalidates it.
# ---------------------------------------------------------------------------


@pytest.fixture
async def b5_app_client(
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[object, AsyncClient]]:
    """Like ``b5_client`` but also hands back the ``app`` so a test can inspect
    ``app.state.agent_runtime`` — the write paths must evict its build cache."""
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        yield app, client


def _seed_build_cache(app: object) -> tuple[object, str, str, str, None]:
    """Stand a sentinel build in the runtime cache for code-reviewer@1.0.0 so a
    following write can be shown to evict it. Returns the cache key.

    The shape must match ``runtime._CacheKey`` — the invalidators match
    positionally (tenant at ``k[0]``, OAuth subject at ``k[4]``), so a key of the
    wrong arity would either be dropped for the wrong reason or, once the sha
    element moved, stop representing anything the runtime can actually hold."""
    runtime = app.state.agent_runtime  # type: ignore[attr-defined]
    key = (_DEFAULT_TENANT, "code-reviewer", "1.0.0", "0" * 64, None)
    runtime._cache[key] = object()  # stand-in for a BuiltAgent
    return key


@pytest.mark.asyncio
async def test_put_invalidates_runtime_build_cache(
    b5_app_client: tuple[object, AsyncClient],
) -> None:
    app, client = b5_app_client
    await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    key = _seed_build_cache(app)

    updated = _VALID_YAML.replace("you are a reviewer", "you are a strict reviewer")
    resp = await _put_manifest(client, "code-reviewer", "1.0.0", updated)
    assert resp.status_code == 200
    # Stale build evicted → the next run rebuilds from the edited spec.
    assert key not in app.state.agent_runtime._cache  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rollback_invalidates_runtime_build_cache(
    b5_app_client: tuple[object, AsyncClient],
) -> None:
    app, client = b5_app_client
    await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    await _put_manifest(client, "code-reviewer", "1.0.0", _UPDATED_YAML)
    key = _seed_build_cache(app)

    resp = await client.post("/v1/agents/code-reviewer/1.0.0/revisions/1/rollback")
    assert resp.status_code == 200
    assert key not in app.state.agent_runtime._cache  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_delete_invalidates_runtime_build_cache(
    b5_app_client: tuple[object, AsyncClient],
) -> None:
    app, client = b5_app_client
    await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    key = _seed_build_cache(app)

    resp = await client.delete("/v1/agents/code-reviewer/1.0.0")
    assert resp.status_code == 204
    # A re-register at the same (name, version) must not reuse the deleted build.
    assert key not in app.state.agent_runtime._cache  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Deletion hygiene PR4 — delete cascade (cancel in-flight runs + disable
# triggers + audit counts / failure booleans)
# ---------------------------------------------------------------------------


class _CascadeCtx:
    """Delete-cascade test context — app + the stores the cascade touches."""

    def __init__(
        self,
        *,
        client: AsyncClient,
        app: object,
        tenant_id: UUID,
        run_store: InMemoryRunStore,
        audit_store: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.run_store = run_store
        self.audit_store = audit_store


@pytest.fixture
async def cascade_ctx() -> AsyncIterator[_CascadeCtx]:
    """Like ``b5_client`` but with the run / thread stores explicitly shared
    between the runtime's RunManager and ``app.state`` (the ``disable_agent``
    test wiring) so the delete cascade's run-cancel loop is observable."""
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    threads = InMemoryThreadMetaStore()
    run_store = InMemoryRunStore(thread_meta_store=threads)
    run_event_store = InMemoryRunEventStore()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        thread_meta_repo=threads,
    )
    tenant_id = uuid4()
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=tenant_id)}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        resp = await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        assert resp.status_code == 201, resp.text
        yield _CascadeCtx(
            client=client,
            app=app,
            tenant_id=tenant_id,
            run_store=run_store,
            audit_store=audit_store,
        )


async def _seed_trigger(
    store: TriggerStore,
    *,
    tenant_id: UUID,
    agent_name: str,
    agent_version: str,
    name: str,
) -> TriggerRecord:
    now = datetime.now(UTC)
    record = TriggerRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_name=agent_name,
        agent_version=agent_version,
        name=name,
        kind="cron",
        config={"expr": "0 9 * * *"},
        enabled=True,
        source="api",
        created_at=now,
        updated_at=now,
    )
    await store.create(record)
    return record


async def _manifest_delete_details(ctx: _CascadeCtx) -> dict[str, object]:
    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=1000))
    entries = [e for e in page.entries if e.action is AuditAction.MANIFEST_DELETE]
    assert len(entries) == 1
    return dict(entries[0].details)


@pytest.mark.asyncio
async def test_delete_disables_only_this_versions_triggers(cascade_ctx: _CascadeCtx) -> None:
    store: TriggerStore = cascade_ctx.app.state.trigger_store  # type: ignore[attr-defined]
    target = await _seed_trigger(
        store,
        tenant_id=cascade_ctx.tenant_id,
        agent_name="code-reviewer",
        agent_version="1.0.0",
        name="nightly",
    )
    other_agent = await _seed_trigger(
        store,
        tenant_id=cascade_ctx.tenant_id,
        agent_name="other-agent",
        agent_version="1.0.0",
        name="nightly",
    )
    other_version = await _seed_trigger(
        store,
        tenant_id=cascade_ctx.tenant_id,
        agent_name="code-reviewer",
        agent_version="2.0.0",
        name="weekly",
    )

    resp = await cascade_ctx.client.delete("/v1/agents/code-reviewer/1.0.0")
    assert resp.status_code == 204, resp.text

    target_after = await store.get(trigger_id=target.id, tenant_id=cascade_ctx.tenant_id)
    other_agent_after = await store.get(trigger_id=other_agent.id, tenant_id=cascade_ctx.tenant_id)
    other_version_after = await store.get(
        trigger_id=other_version.id, tenant_id=cascade_ctx.tenant_id
    )
    assert target_after is not None
    assert target_after.enabled is False
    assert other_agent_after is not None
    assert other_agent_after.enabled is True
    assert other_version_after is not None
    assert other_version_after.enabled is True

    details = await _manifest_delete_details(cascade_ctx)
    assert details["triggers_disabled"] == 1
    assert details["runs_cancelled"] == 0
    assert "triggers_disable_failed" not in details
    assert "runs_cancel_failed" not in details


@pytest.mark.asyncio
async def test_delete_cancels_in_flight_runs(cascade_ctx: _CascadeCtx) -> None:
    sess = await cascade_ctx.client.post(
        "/v1/sessions", json={"agent_name": "code-reviewer", "agent_version": "1.0.0"}
    )
    assert sess.status_code == 201, sess.text
    thread_id = UUID(sess.json()["data"]["thread_id"])

    run_manager = cascade_ctx.app.state.agent_runtime.run_manager  # type: ignore[attr-defined]
    run_id = uuid4()
    await run_manager.create(run_id=run_id, thread_id=thread_id, tenant_id=cascade_ctx.tenant_id)
    await run_manager.set_status(run_id, RunStatus.RUNNING)

    resp = await cascade_ctx.client.delete("/v1/agents/code-reviewer/1.0.0")
    assert resp.status_code == 204, resp.text

    info = await cascade_ctx.run_store.get(run_id=run_id, tenant_id=cascade_ctx.tenant_id)
    assert info is not None
    assert info.status is RunStatus.INTERRUPTED

    page = await cascade_ctx.audit_store.query(
        AuditQuery(tenant_id=cascade_ctx.tenant_id, limit=1000)
    )
    cancels = [e for e in page.entries if e.action is AuditAction.SESSION_CANCEL]
    assert len(cancels) == 1
    assert cancels[0].reason == "agent_deleted"
    assert cancels[0].resource_id == str(run_id)

    details = await _manifest_delete_details(cascade_ctx)
    assert details["runs_cancelled"] == 1


@pytest.mark.asyncio
async def test_delete_cancels_only_this_versions_in_flight_runs(
    cascade_ctx: _CascadeCtx,
) -> None:
    """Deletion hygiene follow-up — the cancel is version-level: deleting
    ``code-reviewer@1.0.0`` leaves ``@2.0.0``'s live session running."""
    v2_yaml = _VALID_YAML.replace('version: "1.0.0"', 'version: "2.0.0"')
    reg = await cascade_ctx.client.post("/v1/agents", json={"manifest_yaml": v2_yaml})
    assert reg.status_code == 201, reg.text

    run_manager = cascade_ctx.app.state.agent_runtime.run_manager  # type: ignore[attr-defined]
    runs: dict[str, UUID] = {}
    for version in ("1.0.0", "2.0.0"):
        sess = await cascade_ctx.client.post(
            "/v1/sessions", json={"agent_name": "code-reviewer", "agent_version": version}
        )
        assert sess.status_code == 201, sess.text
        thread_id = UUID(sess.json()["data"]["thread_id"])
        run_id = uuid4()
        await run_manager.create(
            run_id=run_id, thread_id=thread_id, tenant_id=cascade_ctx.tenant_id
        )
        await run_manager.set_status(run_id, RunStatus.RUNNING)
        runs[version] = run_id

    resp = await cascade_ctx.client.delete("/v1/agents/code-reviewer/1.0.0")
    assert resp.status_code == 204, resp.text

    v1_info = await cascade_ctx.run_store.get(run_id=runs["1.0.0"], tenant_id=cascade_ctx.tenant_id)
    v2_info = await cascade_ctx.run_store.get(run_id=runs["2.0.0"], tenant_id=cascade_ctx.tenant_id)
    assert v1_info is not None and v1_info.status is RunStatus.INTERRUPTED
    assert v2_info is not None and v2_info.status is RunStatus.RUNNING  # not collateral damage

    page = await cascade_ctx.audit_store.query(
        AuditQuery(tenant_id=cascade_ctx.tenant_id, limit=1000)
    )
    cancels = [e for e in page.entries if e.action is AuditAction.SESSION_CANCEL]
    assert [e.resource_id for e in cancels] == [str(runs["1.0.0"])]

    details = await _manifest_delete_details(cascade_ctx)
    assert details["runs_cancelled"] == 1


@pytest.mark.asyncio
async def test_delete_survives_trigger_disable_failure(
    cascade_ctx: _CascadeCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort cascade — a trigger-store failure never blocks the delete,
    but MUST be audit-visible (``triggers_disable_failed``)."""

    async def _boom(**_kwargs: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        cascade_ctx.app.state.trigger_store,  # type: ignore[attr-defined]
        "disable_for_agent",
        _boom,
    )

    resp = await cascade_ctx.client.delete("/v1/agents/code-reviewer/1.0.0")
    assert resp.status_code == 204, resp.text

    details = await _manifest_delete_details(cascade_ctx)
    assert details["triggers_disable_failed"] is True
    assert details["triggers_disabled"] == 0


@pytest.mark.asyncio
async def test_delete_survives_run_cancel_failure(
    cascade_ctx: _CascadeCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same audit-visibility guarantee for the run-cancel half
    (``runs_cancel_failed``)."""

    async def _boom(**_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(cascade_ctx.run_store, "list_running_for_agent", _boom)

    resp = await cascade_ctx.client.delete("/v1/agents/code-reviewer/1.0.0")
    assert resp.status_code == 204, resp.text

    details = await _manifest_delete_details(cascade_ctx)
    assert details["runs_cancel_failed"] is True
    assert details["runs_cancelled"] == 0


# ---------------------------------------------------------------------------
# W2 — agent 详情读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套 per endpoint:system_admin 带目标租户 tenant_id → 200;普通租户
# 用户带他租户 tenant_id → 403 TENANT_NOT_ALLOWED;tenant_id=* → 400
# SCOPE_ALL_NOT_SUPPORTED。
# ---------------------------------------------------------------------------


#: (name, path) — the three agent detail read endpoints under scope.
_AGENT_SCOPE_PATHS: list[tuple[str, str]] = [
    ("get_agent", "/v1/agents/code-reviewer/1.0.0"),
    ("revisions", "/v1/agents/code-reviewer/1.0.0/revisions"),
    ("revision_detail", "/v1/agents/code-reviewer/1.0.0/revisions/1"),
]


@pytest.mark.asyncio
async def test_get_agent_system_admin_target_tenant_200(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    headers = await grant_system_admin(b5_client)
    resp = await b5_client.get(
        "/v1/agents/code-reviewer/1.0.0",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["record"]["name"] == "code-reviewer"


@pytest.mark.asyncio
async def test_agent_revisions_system_admin_target_tenant_200(b5_client: AsyncClient) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    headers = await grant_system_admin(b5_client)
    listing = await b5_client.get(
        "/v1/agents/code-reviewer/1.0.0/revisions",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]["items"]
    assert [i["revision"] for i in items] == [1]


@pytest.mark.asyncio
async def test_agent_revision_detail_system_admin_target_tenant_200(
    b5_client: AsyncClient,
) -> None:
    await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    headers = await grant_system_admin(b5_client)
    snap = await b5_client.get(
        "/v1/agents/code-reviewer/1.0.0/revisions/1",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert snap.status_code == 200, snap.text
    assert snap.json()["data"]["record"]["revision"] == 1


@pytest.mark.parametrize("name,path", _AGENT_SCOPE_PATHS)
@pytest.mark.asyncio
async def test_agent_detail_foreign_tenant_user_403(
    b5_client: AsyncClient, name: str, path: str
) -> None:
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await b5_client.get(path, params={"tenant_id": str(_DEFAULT_TENANT)}, headers=foreign)
    assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.parametrize("name,path", _AGENT_SCOPE_PATHS)
@pytest.mark.asyncio
async def test_agent_detail_tenant_id_star_400(
    b5_client: AsyncClient, name: str, path: str
) -> None:
    resp = await b5_client.get(path, params={"tenant_id": "*"})
    assert resp.status_code == 400, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED", name


# ---------------------------------------------------------------------------
# 保存前的 dry-run 构建闸
#
# 两类失败归属不同,答案也不同:manifest 写错了 → 422 拒绝(编辑的人改得了);
# 部署没配好 → 照存 + warning(作者改不了平台状态,拦下去等于把人锁在门外)。
#
# 用注入 builder 的方式而不是造真 manifest:这个测试 app 没配任何凭据,凭据
# 那一关排在技能/模板解析**之前**,所以走真 manifest 永远只会撞上
# PlatformNotConfiguredError —— manifest 错误那条分支在这里不可达。
# ---------------------------------------------------------------------------


async def _ok_builder(spec, *, tenant_id=None, user_id=None):
    """构建永远成功 —— 这批用例验的是并发,不是构建闸。"""
    return object()


def _builder_raising(exc: Exception):
    async def _boom(spec, *, tenant_id=None, user_id=None):
        raise exc

    return _boom


@pytest.mark.asyncio
async def test_create_rejects_a_manifest_that_cannot_be_built(
    b5_app_client: tuple[object, AsyncClient],
) -> None:
    """引用不到的技能这类错误:以前存得进去,然后之后每个 run 都 422。"""
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _builder_raising(  # type: ignore[attr-defined]
        SkillNotFoundError("skill 'no-such-skill' not found for this tenant")
    )

    resp = await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "MANIFEST_UNBUILDABLE"
    assert "no-such-skill" in resp.json()["error"]["message"]

    # 关键:没落库。存进去的坏配置才是这条闸要消灭的东西。
    listed = await client.get("/v1/agents")
    assert all(i["name"] != "code-reviewer" for i in listed.json()["data"]["items"])


@pytest.mark.asyncio
async def test_update_rejects_and_leaves_the_stored_version_untouched(
    b5_app_client: tuple[object, AsyncClient],
) -> None:
    app, client = b5_app_client
    assert (await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})).status_code == 201
    app.state.agent_runtime.agent_builder = _builder_raising(  # type: ignore[attr-defined]
        AgentFactoryError("sub-agent delegation cycle detected: a -> b -> a")
    )

    resp = await _put_manifest(client, "code-reviewer", "1.0.0", _JINJA_YAML)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "MANIFEST_UNBUILDABLE"

    detail = await client.get("/v1/agents/code-reviewer/1.0.0")
    stored = detail.json()["data"]["record"]["spec"]["spec"]["system_prompt"]["template"]
    assert stored == "you are a reviewer", "被拒的写入不能改变库里那一版"


@pytest.mark.asyncio
async def test_platform_gap_saves_with_a_warning_instead_of_refusing(
    b5_client: AsyncClient,
) -> None:
    """平台没配 provider 凭据 → **不能**拒绝。

    作者改不了平台状态。拦下去的后果是全新部署在凭据到位之前一个 Agent 都注册
    不了 —— 而这正是这个测试 app 的状态(它本来就不配凭据)。
    """
    resp = await b5_client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    assert resp.status_code == 201, resp.text
    warning = resp.json()["data"]["build_warning"]
    assert warning is not None
    assert "credential" in warning


@pytest.mark.asyncio
async def test_a_clean_build_carries_no_warning(
    b5_app_client: tuple[object, AsyncClient],
) -> None:
    """反向:warning 不能是恒有的装饰,否则它什么都没说。"""
    app, client = b5_app_client

    async def _ok(spec, *, tenant_id=None, user_id=None):
        return object()

    app.state.agent_runtime.agent_builder = _ok  # type: ignore[attr-defined]

    resp = await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["build_warning"] is None


@pytest.mark.asyncio
async def test_rollback_is_not_gated_by_the_build_check(
    b5_app_client: tuple[object, AsyncClient],
) -> None:
    """回滚是逃生口,不设闸。

    平台侧变动让当前 manifest 建不出来时,操作者必须还能回到已知可用的版本;
    在这条路上设闸等于从里面把门锁死。所以即使 builder 此刻必然抛错,回滚
    也要成功。
    """
    app, client = b5_app_client
    await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    await _put_manifest(client, "code-reviewer", "1.0.0", _JINJA_YAML)
    app.state.agent_runtime.agent_builder = _builder_raising(  # type: ignore[attr-defined]
        AgentFactoryError("whatever is broken right now")
    )

    resp = await client.post("/v1/agents/code-reviewer/1.0.0/revisions/1/rollback")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 并发编辑:If-Match 乐观锁
#
# 两个人同时开着配置页,后点保存的会静默盖掉前一个 —— 当场没有任何提示。
# 历史表里留了档能回滚,但你得先意识到被盖了。
#
# 必填而不是选填:这个端点是 console-only,唯一调用方就是控制台,没有对外
# 契约要破;而选填的安全闸总有人忘,忘了就退回静默覆盖。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_without_if_match_is_refused(b5_app_client) -> None:
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})

    resp = await client.put("/v1/agents/code-reviewer/1.0.0", json={"manifest_yaml": _JINJA_YAML})
    assert resp.status_code == 428, resp.text
    assert resp.json()["error"]["code"] == "MANIFEST_IF_MATCH_REQUIRED"


@pytest.mark.asyncio
async def test_update_with_a_stale_if_match_is_a_conflict(b5_app_client) -> None:
    """B 拿着 A 保存前的版本去写 —— 必须撞墙,不能静默盖掉 A。"""
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    created = await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    both_loaded = created.json()["data"]["record"]["spec_sha256"]

    # A 先保存成功。
    first = await client.put(
        "/v1/agents/code-reviewer/1.0.0",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": both_loaded},
    )
    assert first.status_code == 200, first.text

    # B 手里还是打开页面时那个 sha。
    second = await client.put(
        "/v1/agents/code-reviewer/1.0.0",
        json={"manifest_yaml": _VALID_YAML},
        headers={"If-Match": both_loaded},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "MANIFEST_STALE_WRITE"

    # A 的那一版还在,没被 B 盖掉。
    detail = await client.get("/v1/agents/code-reviewer/1.0.0")
    assert (
        detail.json()["data"]["record"]["spec_sha256"]
        == first.json()["data"]["record"]["spec_sha256"]
    )


@pytest.mark.asyncio
async def test_update_with_a_current_if_match_succeeds(b5_app_client) -> None:
    """反向:锁不能把正常保存也拦了。"""
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    created = await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})

    resp = await client.put(
        "/v1/agents/code-reviewer/1.0.0",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": created.json()["data"]["record"]["spec_sha256"]},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_update_accepts_a_quoted_etag(b5_app_client) -> None:
    """HTTP 的 ETag 语法带引号。curl 用户照标准写不该被当成不匹配。"""
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    created = await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    sha = created.json()["data"]["record"]["spec_sha256"]

    resp = await client.put(
        "/v1/agents/code-reviewer/1.0.0",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": f'"{sha}"'},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 草稿 / 发布:把「改配置」和「让改动生效」分成两个动作
# ---------------------------------------------------------------------------


async def _create(client, *, builder=None) -> str:
    """建一个 Agent,返回它当前的 sha。"""
    created = await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
    assert created.status_code == 201, created.text
    return created.json()["data"]["record"]["spec_sha256"]


@pytest.mark.asyncio
async def test_saving_a_draft_does_not_touch_the_live_spec(b5_app_client) -> None:
    """草稿的全部意义:存了不生效。"""
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)

    saved = await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": live_sha},
    )
    assert saved.status_code == 200, saved.text

    detail = (await client.get("/v1/agents/code-reviewer/1.0.0")).json()["data"]["record"]
    assert detail["spec_sha256"] == live_sha, "线上那一版必须一个字节都没动"
    assert detail["draft"] is not None
    assert detail["draft"]["spec"]["spec"]["system_prompt"]["jinja"] is True
    # 存草稿不是一次改动,不该记历史。
    history = (await client.get("/v1/agents/code-reviewer/1.0.0/revisions")).json()
    assert len(history["data"]["items"]) == 1


@pytest.mark.asyncio
async def test_publish_makes_the_draft_live_and_clears_it(b5_app_client) -> None:
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)
    await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": live_sha},
    )

    published = await client.post(
        "/v1/agents/code-reviewer/1.0.0/publish", headers={"If-Match": live_sha}
    )
    assert published.status_code == 200, published.text
    record = published.json()["data"]["record"]
    assert record["spec_sha256"] != live_sha
    assert record["draft"] is None, "发布后编辑缓冲区必须空,否则页面会一直提示有草稿"

    history = (await client.get("/v1/agents/code-reviewer/1.0.0/revisions")).json()
    assert [r["revision"] for r in history["data"]["items"]] == [2, 1]


@pytest.mark.asyncio
async def test_draft_save_reports_an_unbuildable_manifest_without_refusing(
    b5_app_client,
) -> None:
    """草稿是半成品 —— 拦住一次保存等于逼人把改了一半的东西丢掉。

    但必须**说出来**,否则人会一路点到发布才发现。
    """
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)
    app.state.agent_runtime.agent_builder = _builder_raising(  # type: ignore[attr-defined]
        SkillNotFoundError("skill 'nope' not found for this tenant")
    )

    saved = await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": live_sha},
    )
    assert saved.status_code == 200, "草稿保存不该被构建失败拦住"
    assert "nope" in saved.json()["data"]["build_error"]
    # 确实存下来了。
    detail = (await client.get("/v1/agents/code-reviewer/1.0.0")).json()["data"]["record"]
    assert detail["draft"] is not None


@pytest.mark.asyncio
async def test_publish_refuses_an_unbuildable_draft(b5_app_client) -> None:
    """发布是另一回事:建不出来的东西推上线,受害的是下一个发消息的用户。"""
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)
    await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": live_sha},
    )
    app.state.agent_runtime.agent_builder = _builder_raising(  # type: ignore[attr-defined]
        AgentFactoryError("sub-agent delegation cycle detected")
    )

    resp = await client.post(
        "/v1/agents/code-reviewer/1.0.0/publish", headers={"If-Match": live_sha}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "MANIFEST_UNBUILDABLE"

    detail = (await client.get("/v1/agents/code-reviewer/1.0.0")).json()["data"]["record"]
    assert detail["spec_sha256"] == live_sha, "被拒的发布不能改变线上那一版"
    assert detail["draft"] is not None, "草稿也要留着,否则等于替人把改动扔了"


@pytest.mark.asyncio
async def test_publish_without_a_draft_is_a_conflict(b5_app_client) -> None:
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)

    resp = await client.post(
        "/v1/agents/code-reviewer/1.0.0/publish", headers={"If-Match": live_sha}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "MANIFEST_NO_DRAFT"


@pytest.mark.asyncio
async def test_discard_removes_the_draft_and_leaves_live_alone(b5_app_client) -> None:
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)
    saved = await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": live_sha},
    )
    draft_sha = saved.json()["data"]["record"]["draft"]["spec_sha256"]

    resp = await client.request(
        "DELETE",
        "/v1/agents/code-reviewer/1.0.0/draft",
        headers={"If-Match": draft_sha},
    )
    assert resp.status_code == 200, resp.text
    detail = (await client.get("/v1/agents/code-reviewer/1.0.0")).json()["data"]["record"]
    assert detail["draft"] is None
    assert detail["spec_sha256"] == live_sha


@pytest.mark.asyncio
async def test_second_editor_cannot_clobber_an_existing_draft(b5_app_client) -> None:
    """有草稿之后,``If-Match`` 要匹配的是**草稿**的 sha ——「匹配你载入编辑器
    的那一版」。拿线上 sha 来写 = 你没看见别人的草稿。"""
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)
    await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": live_sha},
    )

    second = await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _VALID_YAML},
        headers={"If-Match": live_sha},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "MANIFEST_STALE_WRITE"


@pytest.mark.asyncio
async def test_draft_endpoints_require_if_match(b5_app_client) -> None:
    app, client = b5_app_client
    app.state.agent_runtime.agent_builder = _ok_builder  # type: ignore[attr-defined]
    live_sha = await _create(client)
    # publish 先判「有没有草稿」再判前置条件(目标状态比协议合规更值得说,与
    # update_agent 里 404/越权优先于 If-Match 同一条原则),所以这里得先有草稿
    # 才验得到 428。
    await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft",
        json={"manifest_yaml": _JINJA_YAML},
        headers={"If-Match": live_sha},
    )

    blind_save = await client.put(
        "/v1/agents/code-reviewer/1.0.0/draft", json={"manifest_yaml": _JINJA_YAML}
    )
    blind_discard = await client.request("DELETE", "/v1/agents/code-reviewer/1.0.0/draft")
    blind_publish = await client.post("/v1/agents/code-reviewer/1.0.0/publish")
    for resp in (blind_save, blind_discard, blind_publish):
        assert resp.status_code == 428, resp.text
        assert resp.json()["error"]["code"] == "MANIFEST_IF_MATCH_REQUIRED"
