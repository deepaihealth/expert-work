"""Stream 8.5 — agents routes enforce instance-level RBAC + ABAC.

End-to-end: an admin grants an operator a CONDITIONED role binding (restricted
to specific agents by id / label), and that operator can act only on the
matching agent instances — proving the conditioned binding grants instance-level
access (not the type-wide access an unconditioned operator role would).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


def _agent_yaml(name: str, *, team: str | None = None) -> str:
    labels = f"\n  labels:\n    team: {team}" if team else ""
    return f"""\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: {name}
  version: "1.0.0"
  tenant: platform-eng{labels}
spec:
  tenant_config: {{}}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you are an agent"
  sandbox:
    resources: {{ cpu: "1.0", memory: "1Gi" }}
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


@pytest.fixture
async def app_transport() -> AsyncIterator[ASGITransport]:
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
    yield ASGITransport(app=app)


def _client(transport: ASGITransport, token: str) -> AsyncClient:
    return AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _seed_agents_and_binding(
    transport: ASGITransport, *, operator_id, conditions: dict
) -> None:
    """As admin: create two agents (one labelled) + a conditioned operator binding."""
    admin = _client(transport, make_test_jwt(tenant_id=_TENANT))
    async with admin:
        r1 = await admin.post(
            "/v1/agents", json={"manifest_yaml": _agent_yaml("agent-foo", team="支持")}
        )
        assert r1.status_code == 201, r1.text
        r2 = await admin.post("/v1/agents", json={"manifest_yaml": _agent_yaml("agent-bar")})
        assert r2.status_code == 201, r2.text
        rb = await admin.post(
            "/v1/role_bindings",
            json={
                "subject_type": "user",
                "subject_id": str(operator_id),
                "role": "operator",
                "conditions": conditions,
            },
        )
        assert rb.status_code == 201, rb.text


@pytest.mark.asyncio
async def test_resource_ids_condition_allows_only_listed_agent(
    app_transport: ASGITransport,
) -> None:
    operator_id = uuid4()
    await _seed_agents_and_binding(
        app_transport, operator_id=operator_id, conditions={"resource_ids": ["agent-foo"]}
    )
    # Operator carries NO realm role — access comes solely from the conditioned
    # binding, so it is restricted to the listed instance.
    op_token = make_test_jwt(tenant_id=_TENANT, subject=str(operator_id), roles=())
    op = _client(app_transport, op_token)
    async with op:
        allowed = await op.get("/v1/agents/agent-foo/1.0.0")
        forbidden = await op.get("/v1/agents/agent-bar/1.0.0")
    assert allowed.status_code == 200, allowed.text
    assert forbidden.status_code == 403, forbidden.text


@pytest.mark.asyncio
async def test_label_condition_allows_only_matching_agent(
    app_transport: ASGITransport,
) -> None:
    operator_id = uuid4()
    await _seed_agents_and_binding(
        app_transport, operator_id=operator_id, conditions={"labels": {"team": "支持"}}
    )
    op_token = make_test_jwt(tenant_id=_TENANT, subject=str(operator_id), roles=())
    op = _client(app_transport, op_token)
    async with op:
        allowed = await op.get("/v1/agents/agent-foo/1.0.0")  # labelled team=支持
        forbidden = await op.get("/v1/agents/agent-bar/1.0.0")  # no label
    assert allowed.status_code == 200, allowed.text
    assert forbidden.status_code == 403, forbidden.text


@pytest.mark.asyncio
async def test_conditioned_operator_write_scoped_to_listed_agent(
    app_transport: ASGITransport,
) -> None:
    operator_id = uuid4()
    await _seed_agents_and_binding(
        app_transport, operator_id=operator_id, conditions={"resource_ids": ["agent-foo"]}
    )
    op_token = make_test_jwt(tenant_id=_TENANT, subject=str(operator_id), roles=())
    op = _client(app_transport, op_token)
    async with op:
        # Operator holds manifest:write — PUT (update) is the write action.
        forbidden = await op.put(
            "/v1/agents/agent-bar/1.0.0", json={"manifest_yaml": _agent_yaml("agent-bar")}
        )
        # 更新要带 If-Match(并发编辑保护)。注意 ``forbidden`` 那条**不**需要:
        # 授权在前置条件之前判,所以越权写拿到的是 403 而不是「你没带头」。
        current = await op.get("/v1/agents/agent-foo/1.0.0")
        allowed = await op.put(
            "/v1/agents/agent-foo/1.0.0",
            json={"manifest_yaml": _agent_yaml("agent-foo", team="支持")},
            headers={"If-Match": current.json()["data"]["record"]["spec_sha256"]},
        )
    assert forbidden.status_code == 403, forbidden.text
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_admin_unaffected_by_abac(app_transport: ASGITransport) -> None:
    """Regression — an unconditioned admin keeps type-wide access."""
    operator_id = uuid4()
    await _seed_agents_and_binding(
        app_transport, operator_id=operator_id, conditions={"resource_ids": ["agent-foo"]}
    )
    admin = _client(app_transport, make_test_jwt(tenant_id=_TENANT))
    async with admin:
        a = await admin.get("/v1/agents/agent-foo/1.0.0")
        b = await admin.get("/v1/agents/agent-bar/1.0.0")
    assert a.status_code == 200
    assert b.status_code == 200
