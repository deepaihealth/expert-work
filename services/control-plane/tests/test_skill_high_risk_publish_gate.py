"""Capability Uplift Sprint #3 — Mini-ADR U-24 high-risk publish gate.

Verifies that PATCH /v1/skills/{id} status=active rejects non-admin
actors when the latest version is high_risk (tool_names ∩
{exec_python, http, exec_shell} ≠ ∅ or supporting_files path starts
with ``scripts/``), and that admin / system_admin role allows
activation with the SKILL_HIGH_RISK_ACTIVATED audit trail.

The gate under test is about *high_risk vs not*, so it needs the weakest
role that can still mutate a skill at all. 阶段 1.5 made that role
``operator``: skill routes now carry ``require("manifest", …)``, so a
``viewer`` — which these cases used to use — is stopped by the role gate
before the high-risk check ever runs, and the case would pass for the
wrong reason. (This module's original note claimed "all skill mutations
are admin-only"; that was the *intent*, never enforced — skills.py
carried no route-level authorization until 阶段 1.5.)

See ``docs/streams/STREAM-UPLIFT-DESIGN.md`` § 4.3.12.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


def _settings() -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def _admin_headers() -> dict[str, str]:
    """Tenant ADMIN actor — admin-or-system_admin is U-24-authorized."""
    return {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_TENANT, subject="admin-user", roles=("admin",))
    }


def _operator_headers() -> dict[str, str]:
    """VIEWER actor — U-24 rejects."""
    return {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_TENANT, subject="operator-user", roles=("operator",))
    }


@pytest.fixture
async def app_client() -> AsyncIterator[tuple[AsyncClient, InMemoryAuditLogStore]]:
    audit_store = InMemoryAuditLogStore()
    audit_logger = build_default_audit_logger(audit_store)
    app = create_app(
        settings=_settings(),
        audit_logger=audit_logger,
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield client, audit_store


async def _create_skill_with_version(
    client: AsyncClient,
    *,
    name: str,
    tool_names: list[str],
) -> str:
    """Admin creates a skill + one version with the given tool_names.
    Returns the skill_id."""
    admin = _admin_headers()
    resp = await client.post(
        "/v1/skills",
        json={"name": name, "description": "x", "category": "data"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    skill_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/skills/{skill_id}/versions",
        json={
            "prompt_fragment": "you are a helper",
            "tool_names": tool_names,
            "description": "v1",
            "authored_by": "human",
        },
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return skill_id


@pytest.mark.asyncio
async def test_operator_cannot_activate_high_risk_skill_with_exec_python(
    app_client: tuple[AsyncClient, InMemoryAuditLogStore],
) -> None:
    client, audit_store = app_client
    skill_id = await _create_skill_with_version(
        client, name="dangerous-py", tool_names=["exec_python"]
    )

    response = await client.patch(
        f"/v1/skills/{skill_id}",
        json={"status": "active"},
        headers=_operator_headers(),
    )
    assert response.status_code == 403
    assert "high-risk skill requires tenant admin" in response.json()["detail"]

    page = await audit_store.query(
        AuditQuery(
            tenant_id=_TENANT,
            action=AuditAction.SKILL_HIGH_RISK_ACTIVATION_BLOCKED,
        )
    )
    assert len(page.entries) == 1
    assert "exec_python" in page.entries[0].details["tool_names"]


@pytest.mark.asyncio
async def test_viewer_cannot_activate_high_risk_skill_with_http(
    app_client: tuple[AsyncClient, InMemoryAuditLogStore],
) -> None:
    client, _ = app_client
    skill_id = await _create_skill_with_version(client, name="dangerous-http", tool_names=["http"])

    response = await client.patch(
        f"/v1/skills/{skill_id}",
        json={"status": "active"},
        headers=_operator_headers(),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_activate_high_risk_skill_with_audit_trail(
    app_client: tuple[AsyncClient, InMemoryAuditLogStore],
) -> None:
    client, audit_store = app_client
    skill_id = await _create_skill_with_version(
        client, name="dangerous-admin", tool_names=["exec_python"]
    )

    response = await client.patch(
        f"/v1/skills/{skill_id}",
        json={"status": "active"},
        headers=_admin_headers(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"

    activated = (
        await audit_store.query(
            AuditQuery(
                tenant_id=_TENANT,
                action=AuditAction.SKILL_HIGH_RISK_ACTIVATED,
            )
        )
    ).entries
    assert len(activated) == 1
    blocked = (
        await audit_store.query(
            AuditQuery(
                tenant_id=_TENANT,
                action=AuditAction.SKILL_HIGH_RISK_ACTIVATION_BLOCKED,
            )
        )
    ).entries
    assert len(blocked) == 0


@pytest.mark.asyncio
async def test_operator_can_activate_low_risk_skill(
    app_client: tuple[AsyncClient, InMemoryAuditLogStore],
) -> None:
    """Low-risk skill is NOT gated — confirms the gate is scoped to
    high_risk only (does not regress existing M0 flow)."""
    client, audit_store = app_client
    skill_id = await _create_skill_with_version(
        client, name="safe-skill", tool_names=["log_viewer"]
    )

    response = await client.patch(
        f"/v1/skills/{skill_id}",
        json={"status": "active"},
        headers=_operator_headers(),
    )
    assert response.status_code == 200

    blocked = (
        await audit_store.query(
            AuditQuery(
                tenant_id=_TENANT,
                action=AuditAction.SKILL_HIGH_RISK_ACTIVATION_BLOCKED,
            )
        )
    ).entries
    activated = (
        await audit_store.query(
            AuditQuery(
                tenant_id=_TENANT,
                action=AuditAction.SKILL_HIGH_RISK_ACTIVATED,
            )
        )
    ).entries
    assert blocked == [] and activated == []


@pytest.mark.asyncio
async def test_status_change_to_draft_or_archived_not_gated(
    app_client: tuple[AsyncClient, InMemoryAuditLogStore],
) -> None:
    """Gate fires only on transition to ACTIVE. Archived/draft pass through."""
    client, _ = app_client
    skill_id = await _create_skill_with_version(
        client, name="archived-risk", tool_names=["exec_python"]
    )

    # Use admin to first push to active, then viewer can archive.
    await client.patch(
        f"/v1/skills/{skill_id}", json={"status": "active"}, headers=_admin_headers()
    )

    response = await client.patch(
        f"/v1/skills/{skill_id}", json={"status": "archived"}, headers=_operator_headers()
    )
    # Viewer can archive (no gate on archived).
    assert response.status_code == 200
