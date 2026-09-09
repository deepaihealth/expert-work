"""Tests for ``POST /v1/sessions/{thread_id}/feedback`` — Stream G.6 (#63 / #65)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.feedback_store import InMemoryFeedbackStore
from expert_work.protocol import AuditQuery
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_DEFAULT_TENANT = DEFAULT_DEV_TENANT_ID

#: 固定值而非 ``uuid4()`` —— 它会进 parametrize id,随机值会让 xdist 各 worker
#: 收集到不同的用例集(见 test_console_lockdown.py 同款注释)。
_RUN_ID = "00000000-0000-4000-8000-000000000021"


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
def feedback_store() -> InMemoryFeedbackStore:
    return InMemoryFeedbackStore()


@pytest.fixture
async def client(
    audit_store: InMemoryAuditLogStore,
    feedback_store: InMemoryFeedbackStore,
) -> AsyncIterator[AsyncClient]:
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
        feedback_repo=feedback_store,
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=headers,
    ) as http_client:
        yield http_client


@pytest.mark.asyncio
async def test_submit_feedback_persists_and_correlates(
    client: AsyncClient,
    feedback_store: InMemoryFeedbackStore,
    audit_store: InMemoryAuditLogStore,
) -> None:
    """#63 — feedback lands with the thread / turn correlation + an audit row."""
    thread_id = uuid4()
    response = await client.post(
        f"/v1/sessions/{thread_id}/feedback",
        json={"rating": "up", "comment": "great answer", "turn_seq": 3, "run_id": str(uuid4())},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["rating"] == "up"
    assert body["turn_seq"] == 3
    assert body["id"] is not None
    assert "trace_id" in body  # field is plumbed; value is None without live tracing

    rows = await feedback_store.list_for_thread(thread_id=thread_id)
    assert len(rows) == 1
    assert rows[0].rating == "up"
    assert rows[0].comment == "great answer"
    assert rows[0].turn_seq == 3
    assert rows[0].tenant_id == _DEFAULT_TENANT
    assert rows[0].actor_id

    # Audit emitted feedback:create — the free-text comment never enters
    # the audit trail (it lives in the feedback table).
    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    feedback_audits = [r for r in page.entries if r.action.value == "feedback:create"]
    assert len(feedback_audits) == 1
    assert "great answer" not in str(feedback_audits[0].details)


@pytest.mark.asyncio
async def test_submit_feedback_down_without_comment(
    client: AsyncClient,
    feedback_store: InMemoryFeedbackStore,
) -> None:
    thread_id = uuid4()
    response = await client.post(
        f"/v1/sessions/{thread_id}/feedback",
        json={"rating": "down", "run_id": str(uuid4())},
    )
    assert response.status_code == 201
    rows = await feedback_store.list_for_thread(thread_id=thread_id)
    assert rows[0].rating == "down"
    assert rows[0].comment is None
    assert rows[0].turn_seq is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_body",
    [
        {},  # missing rating
        {"rating": "sideways", "run_id": _RUN_ID},  # not up/down
        {"rating": "up", "unexpected": 1, "run_id": _RUN_ID},  # extra field forbidden
        {"rating": "up", "turn_seq": -1, "run_id": _RUN_ID},  # negative turn_seq
        {"rating": "up"},  # P-2:漏 run_id 也是 422
    ],
)
async def test_submit_feedback_rejects_bad_input(
    client: AsyncClient,
    bad_body: dict[str, object],
) -> None:
    """#65 — input validation rejects malformed bodies with 422."""
    response = await client.post(f"/v1/sessions/{uuid4()}/feedback", json=bad_body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_console_feedback_is_run_scoped_upsert(
    client: AsyncClient, feedback_store: InMemoryFeedbackStore
) -> None:
    thread_id, run_id = uuid4(), uuid4()
    first = await client.post(
        f"/v1/sessions/{thread_id}/feedback",
        json={"rating": "down", "comment": "bad", "run_id": str(run_id)},
    )
    assert first.status_code == 201
    assert first.json()["run_id"] == str(run_id)
    assert first.json()["updated"] is False
    second = await client.post(
        f"/v1/sessions/{thread_id}/feedback", json={"rating": "up", "run_id": str(run_id)}
    )
    assert second.status_code == 201
    assert second.json()["updated"] is True
    rows = await feedback_store.list_for_thread(thread_id=thread_id)
    assert len(rows) == 1
    assert rows[0].rating == "up" and rows[0].run_id == run_id and rows[0].source == "console"
