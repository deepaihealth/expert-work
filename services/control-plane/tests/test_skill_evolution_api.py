"""E2E tests for ``/v1/skill-evolution`` admin API — Stream SE (SE-8-2).

Covers the promote-approval flow (request / review-queue / approve→visibility
flip / reject), eval-evidence + lineage reads, and audit emission. Seeds
agent_private skills directly via the app's in-memory ``SkillStore`` (the public
``POST /v1/skills`` only makes tenant-visible drafts).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery, Role, SkillEvalResult
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_ADMIN = uuid4()  # UUID subject so the decider is a real user id


def _settings() -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject=str(_ADMIN))}"}


Setup = tuple[AsyncClient, FastAPI, InMemoryAuditLogStore]


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_settings(),
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, app, audit_store


async def _seed_agent_private(app: FastAPI, *, name: str) -> str:
    """Create an agent_private skill (v1) in the app store; return its id."""
    store = app.state.skill_store
    skill_id = uuid4()
    await store.create_skill(
        skill_id=skill_id,
        tenant_id=_TENANT,
        name=name,
        visibility="agent_private",
        created_by_user_id=_ADMIN,
        created_by_agent_name="researcher",
    )
    await store.add_version(
        version_id=uuid4(),
        skill_id=skill_id,
        tenant_id=_TENANT,
        prompt_fragment="do the thing",
        authored_by="agent",
        evolution_origin="in_session",
    )
    return str(skill_id)


def _role_headers(role: str) -> dict[str, str]:
    """JWT headers for a non-admin employee (``viewer`` / ``operator``).

    Unlike ``test_skills_api.py``'s helper of the same name, the subject
    must be a real UUID here — ``approve``/``reject`` resolve the decider
    via ``_actor_uuid(request)``, which 403s with a *different* reason
    ("a user identity is required to decide") for a non-UUID subject,
    which would mask the SE-8 owner-gate 403 these tests are asserting on.
    """
    token = make_test_jwt(tenant_id=_TENANT, subject=str(uuid4()), roles=(role,))
    return {"Authorization": f"Bearer {token}"}


async def _seed_tenant_skill(client: AsyncClient, *, name: str) -> str:
    """Create an ordinary ``tenant``-visibility skill (v1) through the public
    admin-UI endpoints — the SE-8 owner gate must never touch this path."""
    create = await client.post("/v1/skills", json={"name": name})
    skill_id = create.json()["id"]
    await client.post(f"/v1/skills/{skill_id}/versions", json={"prompt_fragment": "public prompt"})
    return str(skill_id)


@pytest.mark.asyncio
async def test_request_review_approve_flow(setup: Setup) -> None:
    client, app, audit_store = setup
    sid = await _seed_agent_private(app, name=f"skill-{uuid4().hex[:8]}")

    # open a promote request
    r = await client.post(
        f"/v1/skill-evolution/skills/{sid}/promote-requests",
        json={"skill_version": 1, "reason": "tenant-wide useful"},
    )
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "pending"
    rid = req["id"]

    # it shows up in the review queue
    q = await client.get("/v1/skill-evolution/promote-requests", params={"status": "pending"})
    assert q.status_code == 200
    assert rid in [x["id"] for x in q.json()["items"]]

    # approve → status approved + skill visibility flips to tenant
    a = await client.post(f"/v1/skill-evolution/promote-requests/{rid}/approve", json={})
    assert a.status_code == 200, a.text
    assert a.json()["status"] == "approved"

    skill = (await client.get(f"/v1/skills/{sid}")).json()
    assert skill["visibility"] == "tenant"

    # audit row for the approval
    entries = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    actions = {e.action for e in entries.entries}
    assert AuditAction.SKILL_PROMOTE_REQUESTED in actions
    assert AuditAction.SKILL_PROMOTE_APPROVED in actions


@pytest.mark.asyncio
async def test_reject_keeps_agent_private(setup: Setup) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"skill-{uuid4().hex[:8]}")
    r = await client.post(
        f"/v1/skill-evolution/skills/{sid}/promote-requests", json={"skill_version": 1}
    )
    rid = r.json()["id"]
    rej = await client.post(
        f"/v1/skill-evolution/promote-requests/{rid}/reject",
        json={"decision_reason": "too narrow"},
    )
    assert rej.status_code == 200
    assert rej.json()["status"] == "rejected"
    skill = (await client.get(f"/v1/skills/{sid}")).json()
    assert skill["visibility"] == "agent_private"


@pytest.mark.asyncio
async def test_duplicate_pending_409(setup: Setup) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"skill-{uuid4().hex[:8]}")
    body = {"skill_version": 1}
    assert (
        await client.post(f"/v1/skill-evolution/skills/{sid}/promote-requests", json=body)
    ).status_code == 201
    dup = await client.post(f"/v1/skill-evolution/skills/{sid}/promote-requests", json=body)
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_request_unknown_skill_404(setup: Setup) -> None:
    client, _, _ = setup
    r = await client.post(
        f"/v1/skill-evolution/skills/{uuid4()}/promote-requests", json={"skill_version": 1}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_approve_unknown_request_404(setup: Setup) -> None:
    client, _, _ = setup
    r = await client.post(f"/v1/skill-evolution/promote-requests/{uuid4()}/approve", json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_eval_results_read(setup: Setup) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"skill-{uuid4().hex[:8]}")
    await app.state.skill_store.record_eval_result(
        result=SkillEvalResult(
            id=uuid4(),
            tenant_id=_TENANT,
            skill_id=sid,  # type: ignore[arg-type]
            skill_version=1,
            baseline_score=0.4,
            skill_score=0.85,
            delta=0.45,
            n_cases=12,
            replay_source="trajectory",
            verdict="pass",
            created_at=datetime.now(UTC),
        )
    )
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/eval-results")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["verdict"] == "pass"
    assert items[0]["delta"] == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_lineage_read(setup: Setup) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"skill-{uuid4().hex[:8]}")
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/lineage")
    assert r.status_code == 200
    body = r.json()
    assert body["skill"]["id"] == sid
    assert body["forked_from_source"] is None
    assert len(body["versions"]) == 1
    assert body["versions"][0]["evolution_origin"] == "in_session"


@pytest.mark.asyncio
async def test_lineage_unknown_skill_404(setup: Setup) -> None:
    client, _, _ = setup
    r = await client.get(f"/v1/skill-evolution/skills/{uuid4()}/lineage")
    assert r.status_code == 404


# ── kill-switch (SE-8-3) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_kill_switch_engage_release(setup: Setup) -> None:
    client, _, audit_store = setup
    # initially nothing engaged
    g0 = (await client.get("/v1/skill-evolution/kill-switch")).json()
    assert g0["effective_halted"] is False
    assert g0["tenant"] is None and g0["global"] is None

    # engage tenant scope
    e = await client.post(
        "/v1/skill-evolution/kill-switch/engage",
        json={"scope": "tenant", "reason": "runaway evolution"},
    )
    assert e.status_code == 200, e.text
    assert e.json()["engaged"] is True

    g1 = (await client.get("/v1/skill-evolution/kill-switch")).json()
    assert g1["effective_halted"] is True
    assert g1["tenant"]["engaged"] is True

    # release
    r = await client.post("/v1/skill-evolution/kill-switch/release", json={"scope": "tenant"})
    assert r.status_code == 200 and r.json()["engaged"] is False
    g2 = (await client.get("/v1/skill-evolution/kill-switch")).json()
    assert g2["effective_halted"] is False

    actions = {x.action for x in (await audit_store.query(AuditQuery(tenant_id=_TENANT))).entries}
    assert AuditAction.SKILL_EVOLUTION_KILL_SWITCH_ENGAGED in actions
    assert AuditAction.SKILL_EVOLUTION_KILL_SWITCH_RELEASED in actions


@pytest.mark.asyncio
async def test_global_kill_switch_forbidden_for_tenant_admin(setup: Setup) -> None:
    client, _, _ = setup
    r = await client.post("/v1/skill-evolution/kill-switch/engage", json={"scope": "global"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_global_kill_switch_system_admin() -> None:
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_settings(),
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    sysadmin = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sysadmin,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject=str(sysadmin))}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test", headers=headers) as c:
        e = await c.post("/v1/skill-evolution/kill-switch/engage", json={"scope": "global"})
        assert e.status_code == 200, e.text
        assert e.json()["scope"] == "global" and e.json()["engaged"] is True
        g = (await c.get("/v1/skill-evolution/kill-switch")).json()
        assert g["global"]["engaged"] is True
        assert g["effective_halted"] is True


# ---------------------------------------------------------------------------
# Backlog task 7 (security fix, spec/external-api-v1-p2b) — SE-8 owner gate,
# the three routes task 6's fix missed (C-1 / I-1) plus one found during this
# task's exhaustive scan (C-3, not in the brief).
#
# C-1: ``GET .../lineage`` reused ``skills.py``'s ``_version_dict`` serializer
# (which carries ``prompt_fragment`` — full skill content) with zero owner
# check. I-1: ``GET .../eval-results`` leaked an agent_private skill's
# existence + performance metadata (no content) to any employee who can
# guess/enumerate its id. C-3: ``POST .../promote-requests`` and
# ``.../approve|reject`` operate on the target skill with no owner check at
# all — and ``approve`` flips visibility ``agent_private`` → ``tenant``
# *unconditionally* (``SkillStore.approve_skill_promote``), so a non-admin
# could permanently de-privatize someone else's private skill via
# request+approve. This router has no ``require(role, ...)`` check anywhere
# (only ``console_only()``, which blocks service accounts, not employee
# roles) — the SE-8 owner gate is the only thing standing between a viewer
# JWT and every one of these actions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_lineage_403_for_non_admin_employee(setup: Setup, role: str) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"lineage-priv-{role}")
    headers = _role_headers(role)
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/lineage", headers=headers)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "SKILL_SCOPE_FORBIDDEN"
    # The leak this gate closes: the private prompt body must not be
    # anywhere in the response, in any shape.
    assert "do the thing" not in r.text
    assert "prompt_fragment" not in r.text


@pytest.mark.asyncio
async def test_lineage_admin_not_forbidden(setup: Setup) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name="lineage-priv-admin")
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/lineage")
    assert r.status_code == 200, r.text
    assert r.json()["versions"][0]["prompt_fragment"] == "do the thing"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_lineage_tenant_visibility_unaffected(setup: Setup, role: str) -> None:
    """Regression guard (biggest risk of this change) — an ordinary
    tenant-visibility skill's lineage must stay readable by any employee."""
    client, _, _ = setup
    sid = await _seed_tenant_skill(client, name=f"lineage-tenant-{role}")
    headers = _role_headers(role)
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/lineage", headers=headers)
    assert r.status_code == 200, f"{role}: {r.status_code} {r.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_eval_results_403_for_non_admin_employee(setup: Setup, role: str) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"eval-priv-{role}")
    await app.state.skill_store.record_eval_result(
        result=SkillEvalResult(
            id=uuid4(),
            tenant_id=_TENANT,
            skill_id=sid,  # type: ignore[arg-type]
            skill_version=1,
            baseline_score=0.4,
            skill_score=0.85,
            delta=0.45,
            n_cases=12,
            replay_source="trajectory",
            verdict="pass",
            created_at=datetime.now(UTC),
        )
    )
    headers = _role_headers(role)
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/eval-results", headers=headers)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "SKILL_SCOPE_FORBIDDEN"
    assert "pass" not in r.text


@pytest.mark.asyncio
async def test_eval_results_admin_not_forbidden(setup: Setup) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name="eval-priv-admin")
    await app.state.skill_store.record_eval_result(
        result=SkillEvalResult(
            id=uuid4(),
            tenant_id=_TENANT,
            skill_id=sid,  # type: ignore[arg-type]
            skill_version=1,
            baseline_score=0.4,
            skill_score=0.85,
            delta=0.45,
            n_cases=12,
            replay_source="trajectory",
            verdict="pass",
            created_at=datetime.now(UTC),
        )
    )
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/eval-results")
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["verdict"] == "pass"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_eval_results_tenant_visibility_unaffected(setup: Setup, role: str) -> None:
    client, _, _ = setup
    sid = await _seed_tenant_skill(client, name=f"eval-tenant-{role}")
    headers = _role_headers(role)
    r = await client.get(f"/v1/skill-evolution/skills/{sid}/eval-results", headers=headers)
    assert r.status_code == 200, f"{role}: {r.status_code} {r.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_request_promote_403_for_non_admin_employee_agent_private(
    setup: Setup, role: str
) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"promote-priv-{role}")
    headers = _role_headers(role)
    r = await client.post(
        f"/v1/skill-evolution/skills/{sid}/promote-requests",
        json={"skill_version": 1},
        headers=headers,
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "SKILL_SCOPE_FORBIDDEN"
    # No row was written: an admin can still open a fresh request afterward
    # without hitting the "one pending request per skill" 409 — proof the
    # forbidden attempt did not create a promote_request row.
    admin_retry = await client.post(
        f"/v1/skill-evolution/skills/{sid}/promote-requests", json={"skill_version": 1}
    )
    assert admin_retry.status_code == 201, admin_retry.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_approve_promote_403_for_non_admin_employee_agent_private(
    setup: Setup, role: str
) -> None:
    """C-3's most severe case: ``approve`` flips visibility agent_private→
    tenant unconditionally. A non-admin must not be able to de-privatize
    someone else's private skill this way."""
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"approve-priv-{role}")
    opened = await client.post(
        f"/v1/skill-evolution/skills/{sid}/promote-requests", json={"skill_version": 1}
    )
    rid = opened.json()["id"]
    headers = _role_headers(role)
    r = await client.post(
        f"/v1/skill-evolution/promote-requests/{rid}/approve", json={}, headers=headers
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "SKILL_SCOPE_FORBIDDEN"
    # The write half of C-3: the skill must still be private.
    skill = (await client.get(f"/v1/skills/{sid}")).json()
    assert skill["visibility"] == "agent_private"
    # ... and the request must still be pending (decision never committed).
    q = await client.get("/v1/skill-evolution/promote-requests", params={"status": "pending"})
    assert rid in [x["id"] for x in q.json()["items"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_reject_promote_403_for_non_admin_employee_agent_private(
    setup: Setup, role: str
) -> None:
    client, app, _ = setup
    sid = await _seed_agent_private(app, name=f"reject-priv-{role}")
    opened = await client.post(
        f"/v1/skill-evolution/skills/{sid}/promote-requests", json={"skill_version": 1}
    )
    rid = opened.json()["id"]
    headers = _role_headers(role)
    r = await client.post(
        f"/v1/skill-evolution/promote-requests/{rid}/reject", json={}, headers=headers
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "SKILL_SCOPE_FORBIDDEN"
    q = await client.get("/v1/skill-evolution/promote-requests", params={"status": "pending"})
    assert rid in [x["id"] for x in q.json()["items"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_promote_flow_tenant_visibility_unaffected(setup: Setup, role: str) -> None:
    """Regression guard (biggest risk of this change) — the C-3 gate must not
    touch the ordinary tenant-visibility promote flow. This matters more here
    than elsewhere: the router has *no* role check of its own anywhere, so
    the SE-8 owner gate is the only thing that could accidentally 403 a
    legitimate non-admin governance action on a public skill."""
    client, _, _ = setup
    sid = await _seed_tenant_skill(client, name=f"promote-tenant-{role}")
    headers = _role_headers(role)
    opened = await client.post(
        f"/v1/skill-evolution/skills/{sid}/promote-requests",
        json={"skill_version": 1},
        headers=headers,
    )
    assert opened.status_code == 201, f"{role}: {opened.status_code} {opened.text}"
    rid = opened.json()["id"]
    approved = await client.post(
        f"/v1/skill-evolution/promote-requests/{rid}/approve", json={}, headers=headers
    )
    assert approved.status_code == 200, f"{role}: {approved.status_code} {approved.text}"
