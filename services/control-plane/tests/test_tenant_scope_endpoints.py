"""Stream N — end-to-end tests for 5 list endpoints integrating ``ensure_tenant_scope``.

Confirms the wiring: each endpoint accepts ``?tenant_id=`` and routes
to ``list_all_tenants`` / ``list_by_tenant`` based on the resolution.
Decision matrix is exhaustively covered in :mod:`test_tenant_scope`;
this file proves the 5 routers + 5 stores actually use it.

Matrix per endpoint:
  - tenant_admin without ``?tenant_id``     → 200, items from home tenant, cross_tenant=False
  - tenant_admin with ``?tenant_id=*``      → 403 CROSS_TENANT_FORBIDDEN
  - system_admin with ``?tenant_id=*``      → 200, items from all tenants, cross_tenant=True
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import (
    AgentMetadata,
    AgentSpec,
    AgentSpecBody,
    ApiKeyScope,
    CandidateStatus,
    CurationCandidateRecord,
    EvalDatasetRecord,
    EvalRunRecord,
    EvalRunStatus,
    EvalTriggeredBy,
    FilesystemSpec,
    MemoryItem,
    MemorySpec,
    ModelSpec,
    NetworkSpec,
    QualityDriftAlertRecord,
    QualityScoreRecord,
    ResourceSpec,
    Role,
    SandboxSpec,
    SystemPromptSpec,
    TenantConfig,
    ThreadStatus,
    TriggerRecord,
)
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT_A = uuid4()
_TENANT_B = uuid4()


def _make_agent_spec(name: str) -> AgentSpec:
    return AgentSpec(
        apiVersion="expert_work.io/v1",
        kind="Agent",
        metadata=AgentMetadata(name=name, version="1.0.0", tenant="t"),
        spec=AgentSpecBody(
            tenant_config=TenantConfig(),
            model=ModelSpec(provider="anthropic", name="claude-sonnet-4-5"),
            system_prompt=SystemPromptSpec(template="x"),
            sandbox=SandboxSpec(
                resources=ResourceSpec(cpu="1.0", memory="1Gi"),
                network=NetworkSpec(egress="proxy", allowlist=["api.anthropic.com"]),
                filesystem=FilesystemSpec(readonly_root=True, writable=["/workspace"]),
            ),
            memory=MemorySpec(),
        ),
    )


@pytest.fixture
async def app_state() -> AsyncIterator[tuple[AsyncClient, UUID]]:
    """App + a system-admin user pre-seeded with platform binding.

    Yields ``(client, system_admin_user_id)`` so each test can issue both
    tenant-admin and system-admin JWTs against the same app.
    """
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
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )

    # Seed cross-tenant rows in 5 stores. Done via direct app.state access
    # because we want known UUIDs to assert against.
    now = datetime.now(UTC)
    repo = app.state.agent_spec_repo
    for tenant in (_TENANT_A, _TENANT_B):
        await repo.create(
            tenant_id=tenant,
            spec=_make_agent_spec(f"agent-in-{tenant.hex[:6]}"),
            spec_sha256="x" * 64,
            created_by="seed",
        )

    skills = app.state.skill_store
    for tenant in (_TENANT_A, _TENANT_B):
        await skills.create_skill(
            skill_id=uuid4(),
            tenant_id=tenant,
            name=f"skill-{tenant.hex[:6]}",
            description="",
            category="test",
        )

    triggers = app.state.trigger_store
    for tenant in (_TENANT_A, _TENANT_B):
        await triggers.create(
            TriggerRecord(
                id=uuid4(),
                tenant_id=tenant,
                user_id=None,
                agent_name=f"agent-in-{tenant.hex[:6]}",
                agent_version="1.0.0",
                name="t1",
                kind="cron",
                config={"expr": "* * * * *"},
                enabled=True,
                source="api",
                webhook_secret_hash=None,
                created_at=now,
                updated_at=now,
            )
        )

    candidates = app.state.curation_candidate_store
    for tenant in (_TENANT_A, _TENANT_B):
        await candidates.upsert(
            CurationCandidateRecord(
                id=uuid4(),
                tenant_id=tenant,
                agent_name=f"agent-in-{tenant.hex[:6]}",
                agent_version="1.0.0",
                thread_id=uuid4(),
                user_id=None,
                trajectory_key=f"k/{tenant.hex}",
                outcome="failed",
                signal="failed_outcome",
                feedback_rating=None,
                status=CandidateStatus.PENDING,
                eval_dataset_id=None,
                detected_at=now,
                reviewed_at=None,
            )
        )

    datasets = app.state.eval_dataset_store
    for tenant in (_TENANT_A, _TENANT_B):
        await datasets.create(
            EvalDatasetRecord(
                id=uuid4(),
                tenant_id=tenant,
                agent_name=f"agent-in-{tenant.hex[:6]}",
                name="case-1",
                input={},
                expected={"x": 1},
                source="trajectory",
                source_trajectory_key=None,
                source_user_id=None,
                created_at=now,
                updated_at=now,
            )
        )

    # ── N.4b: 6 more stores ────────────────────────────────────────────────
    threads = app.state.thread_meta_repo
    thread_by_tenant: dict[UUID, UUID] = {}
    for tenant in (_TENANT_A, _TENANT_B):
        thread_id = uuid4()
        thread_by_tenant[tenant] = thread_id
        await threads.create(
            thread_id=thread_id,
            tenant_id=tenant,
            created_by="seed",
            user_id=None,  # unowned — admin sees these
            agent_name=f"agent-in-{tenant.hex[:6]}",
            agent_version="1.0.0",
        )

    # ── Stream H.3 PR 1: seed one run row per tenant so /v1/runs has rows ──
    from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus

    run_store = app.state.run_store
    for tenant in (_TENANT_A, _TENANT_B):
        await run_store.create(
            RunInfo(
                run_id=uuid4(),
                tenant_id=tenant,
                thread_id=thread_by_tenant[tenant],
                user_id=None,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
        )

    accounts = app.state.service_account_repo
    sa_by_tenant: dict[UUID, UUID] = {}
    for tenant in (_TENANT_A, _TENANT_B):
        sa = await accounts.create(
            tenant_id=tenant,
            name=f"sa-{tenant.hex[:6]}",
            description="",
            created_by="seed",
        )
        sa_by_tenant[tenant] = sa.id

    api_keys = app.state.api_key_repo
    for tenant in (_TENANT_A, _TENANT_B):
        await api_keys.create(
            tenant_id=tenant,
            service_account_id=sa_by_tenant[tenant],
            prefix=f"hk_{tenant.hex[:6]}",
            secret_hash="x" * 64,
            scopes=[ApiKeyScope.READ],
            expires_at=None,
            created_by="seed",
        )

    role_bindings = app.state.role_binding_repo
    for tenant in (_TENANT_A, _TENANT_B):
        await role_bindings.create(
            subject_type="user",
            subject_id=uuid4(),
            tenant_id=tenant,
            role=Role.OPERATOR,
            granted_by="seed",
        )

    # Memory + artifact rows need a user_id; seed a placeholder user.
    user_store = app.state.tenant_user_repo
    seeded_users: dict[UUID, UUID] = {}
    for tenant in (_TENANT_A, _TENANT_B):
        user = await user_store.resolve(
            tenant_id=tenant,
            subject_type="user",
            subject_id=f"seed-user-{tenant.hex[:6]}",
            display_name=None,
        )
        seeded_users[tenant] = user.id

    memory_store = app.state.memory_repo
    for tenant in (_TENANT_A, _TENANT_B):
        await memory_store.write(
            [
                MemoryItem(
                    id=uuid4(),
                    tenant_id=tenant,
                    user_id=seeded_users[tenant],
                    kind="fact",
                    content=f"hello from {tenant.hex[:6]}",
                    embedding=tuple(0.0 for _ in range(8)),
                    created_at=now,
                )
            ]
        )

    artifact_store = app.state.artifact_store
    for tenant in (_TENANT_A, _TENANT_B):
        await artifact_store.save_version(
            tenant_id=tenant,
            user_id=seeded_users[tenant],
            name=f"file-{tenant.hex[:6]}.md",
            kind="document",
            path_in_workspace="/workspace/file.md",
            created_in_thread="seed-thread",
        )

    # ── W4 Task C1 review C-4: 5 aggregate endpoints join the matrix ──────
    knowledge = app.state.knowledge_store
    for tenant in (_TENANT_A, _TENANT_B):
        await knowledge.create_base(tenant_id=tenant, name=f"kb-{tenant.hex[:6]}")

    eval_runs = app.state.eval_run_store
    for tenant in (_TENANT_A, _TENANT_B):
        await eval_runs.create_run(
            EvalRunRecord(
                id=uuid4(),
                tenant_id=tenant,
                suite="m0_baseline",
                status=EvalRunStatus.QUEUED,
                triggered_by=EvalTriggeredBy.MANUAL,
                created_at=now,
            )
        )

    quality_scores = app.state.quality_score_store
    for tenant in (_TENANT_A, _TENANT_B):
        await quality_scores.insert(
            QualityScoreRecord(
                tenant_id=tenant,
                agent_name=f"agent-in-{tenant.hex[:6]}",
                agent_version="1.0.0",
                run_id=uuid4(),
                thread_id=uuid4(),
                overall=4,
                dimensions={"addressed_request": 4, "coherence": 4, "safety": 5},
                rationale="ok",
                judge_model="m",
            )
        )

    drift_alerts = app.state.quality_drift_alert_store
    for tenant in (_TENANT_A, _TENANT_B):
        await drift_alerts.insert(
            QualityDriftAlertRecord(
                tenant_id=tenant,
                agent_name=f"agent-in-{tenant.hex[:6]}",
                recent_mean=3.0,
                baseline_mean=4.0,
                drift_pct=0.25,
                recent_count=12,
                baseline_count=80,
            )
        )

    mcp_servers = app.state.tenant_mcp_server_store
    for tenant in (_TENANT_A, _TENANT_B):
        await mcp_servers.create(
            tenant_id=tenant,
            name=f"mcp-{tenant.hex[:6]}",
            transport="streamable_http",
            url="https://mcp.example.com/mcp",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="seed",
        )

    # Stream H placeholder: silence unused locals when adding more rows.
    _ = ThreadStatus

    # System-admin user pre-seeded with platform-scope binding.
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, sys_admin_id


def _tenant_admin_token() -> str:
    return make_test_jwt(tenant_id=_TENANT_A, subject=str(uuid4()))


def _system_admin_token(sys_admin_id: UUID) -> str:
    return make_test_jwt(tenant_id=_TENANT_A, subject=str(sys_admin_id))


# ---------------------------------------------------------------------------
# Per-endpoint matrix — each test runs the 3-row matrix:
#   1. tenant_admin home   → 200, 1 item (their tenant), cross_tenant=False
#   2. tenant_admin "*"    → 403 CROSS_TENANT_FORBIDDEN
#   3. system_admin "*"    → 200, 2 items (both tenants), cross_tenant=True
# ---------------------------------------------------------------------------


# (name, path, home_count, star_count)
#   home_count = rows visible to tenant_admin home tenant (1 per tenant seeded)
#   star_count = rows visible to system_admin across both tenants (2 seeded)
# memory + artifacts are per-user — the tenant_admin JWT resolves to a fresh
# tenant_user (different from the seeded user that owns the row) so home_count=0;
# cross-tenant aggregates without user filter so star_count=2.
_ENDPOINTS: list[tuple[str, str, int, int]] = [
    ("agents", "/v1/agents", 1, 2),
    ("skills", "/v1/skills", 1, 2),
    ("triggers", "/v1/triggers", 1, 2),
    ("curation", "/v1/curation/candidates", 1, 2),
    ("eval-datasets", "/v1/eval-datasets", 1, 2),
    # ── N.4b ──────────────────────────────────────────────────────────────
    ("service_accounts", "/v1/service_accounts", 1, 2),
    # role_bindings star sees 2 tenant rows + 1 platform-scope (system admin) = 3
    ("role_bindings", "/v1/role_bindings", 1, 3),
    # sessions are per-user now (impersonation removed) — a tenant_admin resolves
    # to a fresh tenant_user that doesn't own the seeded row → home_count=0, like
    # memory/artifacts. Cross-tenant (system_admin *) still aggregates → 2.
    ("sessions", "/v1/sessions", 0, 2),
    ("runs", "/v1/runs", 1, 2),  # Stream H.3 PR 1 — Mini-ADR H-6
    ("conversations", "/v1/conversations", 1, 2),  # conversation-centric IA
    ("memory", "/v1/memory", 0, 2),
    ("artifacts", "/v1/artifacts", 0, 2),
    ("api_keys", "/v1/api_keys", 1, 2),
    # ── W4 Task C1 review C-4: the 5 "*" aggregate list endpoints ─────────
    ("knowledge_bases", "/v1/knowledge/bases", 1, 2),
    ("eval_runs", "/v1/eval-runs", 1, 2),
    ("quality_scores", "/v1/quality/scores", 1, 2),
    ("quality_drift_alerts", "/v1/quality/drift-alerts", 1, 2),
    ("mcp_servers", "/v1/mcp-servers", 1, 2),
]


def _items(body: dict[str, object]) -> list[dict[str, object]]:
    """Some endpoints wrap items in ``data``, others put items at the top level.
    ``/v1/mcp-servers`` returns a bare list under ``data``;
    ``/v1/knowledge/bases`` names its list ``bases``."""
    raw: object
    data = body.get("data")
    if isinstance(data, dict):
        raw = data.get("items", [])
    elif isinstance(data, list):
        raw = data
    else:
        raw = body.get("items", body.get("bases", []))
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _is_cross_tenant(body: dict[str, object]) -> bool:
    data = body.get("data")
    if isinstance(data, dict):
        return bool(data.get("cross_tenant"))
    return bool(body.get("cross_tenant"))


@pytest.mark.parametrize("name,path,home_count,star_count", _ENDPOINTS)
@pytest.mark.asyncio
async def test_tenant_admin_home_tenant_returns_only_home_rows(
    app_state: tuple[AsyncClient, UUID],
    name: str,
    path: str,
    home_count: int,
    star_count: int,
) -> None:
    _ = star_count
    client, _sys = app_state
    headers = {"Authorization": f"Bearer {_tenant_admin_token()}"}
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, f"{name}: {response.text}"
    body = response.json()
    assert _is_cross_tenant(body) is False
    items = _items(body)
    assert len(items) == home_count, f"{name}: expected {home_count}, got {len(items)} — {items}"


@pytest.mark.parametrize("name,path,home_count,star_count", _ENDPOINTS)
@pytest.mark.asyncio
async def test_tenant_admin_star_tenant_id_returns_403(
    app_state: tuple[AsyncClient, UUID],
    name: str,
    path: str,
    home_count: int,
    star_count: int,
) -> None:
    _ = home_count, star_count
    client, _sys = app_state
    headers = {"Authorization": f"Bearer {_tenant_admin_token()}"}
    response = await client.get(f"{path}?tenant_id=*", headers=headers)
    assert response.status_code == 403, f"{name}: {response.status_code} {response.text}"
    body = response.json()
    detail = body.get("detail") or body.get("error", {})
    # FastAPI HTTPException(detail=dict) puts dict under "detail"; check either shape.
    if isinstance(detail, dict):
        assert detail.get("code") == "CROSS_TENANT_FORBIDDEN", f"{name}: {detail}"


@pytest.mark.parametrize("name,path,home_count,star_count", _ENDPOINTS)
@pytest.mark.asyncio
async def test_system_admin_star_tenant_id_returns_all_tenants(
    app_state: tuple[AsyncClient, UUID],
    name: str,
    path: str,
    home_count: int,
    star_count: int,
) -> None:
    _ = home_count
    client, sys_admin_id = app_state
    headers = {"Authorization": f"Bearer {_system_admin_token(sys_admin_id)}"}
    response = await client.get(f"{path}?tenant_id=*", headers=headers)
    assert response.status_code == 200, f"{name}: {response.text}"
    body = response.json()
    assert _is_cross_tenant(body) is True, f"{name}: cross_tenant flag missing"
    items = _items(body)
    assert len(items) == star_count, f"{name}: expected {star_count}, got {len(items)}"


# ---------------------------------------------------------------------------
# Stream N — role_binding platform_scope writes + list filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_admin_cannot_create_platform_scope_binding(
    app_state: tuple[AsyncClient, UUID],
) -> None:
    client, _ = app_state
    headers = {"Authorization": f"Bearer {_tenant_admin_token()}"}
    response = await client.post(
        "/v1/role_bindings",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": str(uuid4()),
            "role": "system_admin",
            "platform_scope": True,
        },
    )
    assert response.status_code == 403, response.text
    detail = response.json().get("detail", {})
    assert detail.get("code") == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_system_admin_can_create_platform_scope_binding(
    app_state: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = app_state
    headers = {"Authorization": f"Bearer {_system_admin_token(sys_admin_id)}"}
    response = await client.post(
        "/v1/role_bindings",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": str(uuid4()),
            "role": "system_admin",
            "platform_scope": True,
        },
    )
    assert response.status_code == 201, response.text
    binding = response.json()["data"]
    assert binding["platform_scope"] is True
    assert binding["tenant_id"] is None
    assert binding["role"] == "system_admin"


@pytest.mark.asyncio
async def test_platform_scope_role_mismatch_rejected_by_validator(
    app_state: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = app_state
    headers = {"Authorization": f"Bearer {_system_admin_token(sys_admin_id)}"}
    response = await client.post(
        "/v1/role_bindings",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": str(uuid4()),
            "role": "admin",  # tenant-scope role, but platform_scope=True
            "platform_scope": True,
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_tenant_admin_platform_scope_list_filter_denied(
    app_state: tuple[AsyncClient, UUID],
) -> None:
    client, _ = app_state
    headers = {"Authorization": f"Bearer {_tenant_admin_token()}"}
    response = await client.get("/v1/role_bindings?platform_scope=true", headers=headers)
    assert response.status_code == 403, response.text
    detail = response.json().get("detail", {})
    assert detail.get("code") == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_system_admin_platform_scope_list_returns_platform_rows(
    app_state: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = app_state
    headers = {"Authorization": f"Bearer {_system_admin_token(sys_admin_id)}"}
    response = await client.get("/v1/role_bindings?platform_scope=true", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    items = _items(body)
    # Fixture pre-seeded 1 platform-scope binding (the test sys_admin user).
    assert len(items) == 1
    assert items[0]["platform_scope"] is True
    assert items[0]["tenant_id"] is None
    assert items[0]["role"] == "system_admin"


# ---------------------------------------------------------------------------
# HX-8 — deployment-level cross-tenant block switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_disabled_blocks_star_and_switch_end_to_end() -> None:
    """`cross_tenant_query_enabled=False` confines system_admin to home:
    both the `*` aggregate and an explicit other-tenant switch 403 with
    CROSS_TENANT_DISABLED, while home-tenant listing keeps working."""
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
        cross_tenant_query_enabled=False,
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        headers = {"Authorization": f"Bearer {_system_admin_token(sys_admin_id)}"}

        star = await client.get("/v1/agents?tenant_id=*", headers=headers)
        assert star.status_code == 403, star.text
        assert star.json()["detail"]["code"] == "CROSS_TENANT_DISABLED"

        switch = await client.get(f"/v1/agents?tenant_id={_TENANT_B}", headers=headers)
        assert switch.status_code == 403, switch.text
        assert switch.json()["detail"]["code"] == "CROSS_TENANT_DISABLED"

        home = await client.get(f"/v1/agents?tenant_id={_TENANT_A}", headers=headers)
        assert home.status_code == 200, home.text
