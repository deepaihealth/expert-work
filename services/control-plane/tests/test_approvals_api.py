"""``/v1/approvals`` queue + batch decide — Stream HX-7 (§ 8.4-PR2).

The list endpoint is read-only over seeded ``agent_approval`` rows; the
batch endpoint shares the resume kernel, so its error semantics (404 /
409 / 422 per item) are what's under test — the happy approve path
needs a genuinely paused graph and stays covered by the J.8 manual /
integration flow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import ApprovalRecord, ApprovalStatus
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_DEFAULT_TENANT = DEFAULT_DEV_TENANT_ID

_AGENT_YAML = """\
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


@pytest.fixture
async def approvals_client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=headers,
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        yield client


async def _create_session(client: AsyncClient) -> str:
    response = await client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["thread_id"])


async def _seed_approval(
    client: AsyncClient,
    thread_id: str,
    *,
    tenant_id: UUID = _DEFAULT_TENANT,
    status: str = "pending",
    requested_at: datetime | None = None,
    summary: str = "approval-gated tool 'send_email'",
    reason_kind: str = "policy_gate",
) -> UUID:
    run_id = uuid4()
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    now = requested_at or datetime.now(UTC)
    await app.state.approval_store.create(
        ApprovalRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            run_id=run_id,
            thread_id=UUID(thread_id),
            request_id=f"approval:{run_id}",
            node="tools",
            reason_kind=reason_kind,  # type: ignore[arg-type]  # tests seed valid values
            action_summary=summary,
            proposed_args={"to": "ops@example.com"},
            requested_at=now,
            timeout_at=now + timedelta(hours=24),
            status=ApprovalStatus(status),
        )
    )
    return run_id


# ---------------------------------------------------------------------------
# GET /v1/approvals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_defaults_to_pending_oldest_first(approvals_client: AsyncClient) -> None:
    thread_id = await _create_session(approvals_client)
    now = datetime.now(UTC)
    newer = await _seed_approval(approvals_client, thread_id, requested_at=now, summary="newer")
    older = await _seed_approval(
        approvals_client, thread_id, requested_at=now - timedelta(hours=2), summary="older"
    )
    await _seed_approval(approvals_client, thread_id, status="approved", summary="decided")

    resp = await approvals_client.get("/v1/approvals")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2  # the decided row is filtered out
    assert [item["run_id"] for item in data["items"]] == [str(older), str(newer)]
    assert data["items"][0]["action_summary"] == "older"
    assert data["items"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_list_filters_by_status(approvals_client: AsyncClient) -> None:
    thread_id = await _create_session(approvals_client)
    decided = await _seed_approval(approvals_client, thread_id, status="rejected")
    await _seed_approval(approvals_client, thread_id, status="pending")

    resp = await approvals_client.get("/v1/approvals", params={"status": "rejected"})

    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["run_id"] == str(decided)


@pytest.mark.asyncio
async def test_list_paginates_with_total(approvals_client: AsyncClient) -> None:
    thread_id = await _create_session(approvals_client)
    now = datetime.now(UTC)
    for i in range(3):
        await _seed_approval(approvals_client, thread_id, requested_at=now + timedelta(minutes=i))

    resp = await approvals_client.get("/v1/approvals", params={"limit": 2, "offset": 2})

    data = resp.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 1
    assert data["limit"] == 2
    assert data["offset"] == 2


@pytest.mark.asyncio
async def test_list_is_tenant_scoped(approvals_client: AsyncClient) -> None:
    thread_id = await _create_session(approvals_client)
    await _seed_approval(approvals_client, thread_id)
    other_tenant_headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}

    resp = await approvals_client.get("/v1/approvals", headers=other_tenant_headers)

    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_list_star_scope_requires_system_admin(approvals_client: AsyncClient) -> None:
    resp = await approvals_client.get("/v1/approvals", params={"tenant_id": "*"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_empty_window_returns_ok(approvals_client: AsyncClient) -> None:
    resp = await approvals_client.get("/v1/approvals")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"items": [], "total": 0, "limit": 100, "offset": 0}


# ---------------------------------------------------------------------------
# POST /v1/approvals:decide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_batch_failures_are_per_item(approvals_client: AsyncClient) -> None:
    """A 404 unknown run and a 409 already-decided never abort the batch."""
    thread_id = await _create_session(approvals_client)
    decided_run = await _seed_approval(approvals_client, thread_id, status="approved")
    unknown_run = uuid4()

    resp = await approvals_client.post(
        "/v1/approvals:decide",
        json={
            "decisions": [
                {"thread_id": thread_id, "run_id": str(unknown_run), "decision": "reject"},
                {"thread_id": thread_id, "run_id": str(decided_run), "decision": "approve"},
            ]
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["succeeded"] == 0
    by_run = {r["run_id"]: r for r in data["results"]}
    assert by_run[str(unknown_run)]["ok"] is False
    assert by_run[str(unknown_run)]["status_code"] == 404
    assert by_run[str(decided_run)]["ok"] is False
    assert by_run[str(decided_run)]["status_code"] == 409


@pytest.mark.asyncio
async def test_decide_modify_without_args_fails_that_item(
    approvals_client: AsyncClient,
) -> None:
    thread_id = await _create_session(approvals_client)
    run_id = await _seed_approval(approvals_client, thread_id)

    resp = await approvals_client.post(
        "/v1/approvals:decide",
        json={"decisions": [{"thread_id": thread_id, "run_id": str(run_id), "decision": "modify"}]},
    )

    data = resp.json()["data"]
    assert data["results"][0]["ok"] is False
    assert data["results"][0]["status_code"] == 422
    # The CAS never fired — the row is still pending and decidable.
    listed = await approvals_client.get("/v1/approvals")
    assert listed.json()["data"]["total"] == 1


@pytest.mark.asyncio
async def test_decide_batch_size_limits(approvals_client: AsyncClient) -> None:
    thread_id = await _create_session(approvals_client)
    too_many = [
        {"thread_id": thread_id, "run_id": str(uuid4()), "decision": "reject"} for _ in range(21)
    ]
    resp = await approvals_client.post("/v1/approvals:decide", json={"decisions": too_many})
    assert resp.status_code == 422

    resp = await approvals_client.post("/v1/approvals:decide", json={"decisions": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_decide_cross_tenant_run_is_404_item(approvals_client: AsyncClient) -> None:
    """A run seeded under another tenant reads as 404, not 403 — no
    cross-tenant existence oracle."""
    thread_id = await _create_session(approvals_client)
    foreign_run = await _seed_approval(approvals_client, thread_id, tenant_id=uuid4())

    resp = await approvals_client.post(
        "/v1/approvals:decide",
        json={
            "decisions": [
                {"thread_id": thread_id, "run_id": str(foreign_run), "decision": "reject"}
            ]
        },
    )

    item = resp.json()["data"]["results"][0]
    assert item["ok"] is False
    assert item["status_code"] == 404


# ---------------------------------------------------------------------------
# B-20 approval triage — kind_class filter + operator-only decide
# ---------------------------------------------------------------------------


async def test_list_filters_by_kind_class(approvals_client: AsyncClient) -> None:
    thread_id = await _create_session(approvals_client)
    now = datetime.now(UTC)
    gate = await _seed_approval(
        approvals_client, thread_id, requested_at=now, reason_kind="policy_gate"
    )
    risk = await _seed_approval(
        approvals_client,
        thread_id,
        requested_at=now + timedelta(minutes=1),
        reason_kind="risk_confirmation",
    )
    question = await _seed_approval(
        approvals_client,
        thread_id,
        requested_at=now + timedelta(minutes=2),
        reason_kind="missing_info",
    )
    fork = await _seed_approval(
        approvals_client,
        thread_id,
        requested_at=now + timedelta(minutes=3),
        reason_kind="approach_choice",
    )

    resp = await approvals_client.get("/v1/approvals", params={"kind_class": "safety"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 2
    assert {item["run_id"] for item in data["items"]} == {str(gate), str(risk)}

    resp = await approvals_client.get("/v1/approvals", params={"kind_class": "clarification"})
    data = resp.json()["data"]
    assert data["total"] == 2
    assert {item["run_id"] for item in data["items"]} == {str(question), str(fork)}

    # No filter — the untabbed view still returns everything.
    resp = await approvals_client.get("/v1/approvals")
    assert resp.json()["data"]["total"] == 4


async def test_list_rejects_unknown_kind_class(approvals_client: AsyncClient) -> None:
    resp = await approvals_client.get("/v1/approvals", params={"kind_class": "bogus"})
    assert resp.status_code == 422


async def test_decide_requires_operator_role(approvals_client: AsyncClient) -> None:
    """B-20 ④ — a viewer JWT must not decide approvals (403, any payload)."""
    viewer = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT, roles=('viewer',))}"}
    resp = await approvals_client.post(
        "/v1/approvals:decide",
        headers=viewer,
        json={
            "decisions": [
                {"thread_id": str(uuid4()), "run_id": str(uuid4()), "decision": "approve"}
            ]
        },
    )
    assert resp.status_code == 403, resp.text


async def test_resume_requires_operator_role(approvals_client: AsyncClient) -> None:
    """B-20 ④ — the single-run resume endpoint carries the same gate."""
    viewer = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT, roles=('viewer',))}"}
    resp = await approvals_client.post(
        f"/v1/sessions/{uuid4()}/runs/{uuid4()}/resume",
        headers=viewer,
        json={"decision": "approve"},
    )
    assert resp.status_code == 403, resp.text


async def test_decide_passes_operator_role(approvals_client: AsyncClient) -> None:
    """Operator clears the gate — a bogus run lands a per-item error, not 403."""
    operator = {
        "Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT, roles=('operator',))}"
    }
    resp = await approvals_client.post(
        "/v1/approvals:decide",
        headers=operator,
        json={
            "decisions": [
                {"thread_id": str(uuid4()), "run_id": str(uuid4()), "decision": "approve"}
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["data"]["results"]
    assert len(results) == 1 and results[0]["ok"] is False
