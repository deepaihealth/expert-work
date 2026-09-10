"""Tests for ``POST /v1/sessions/{thread_id}/feedback`` — Stream G.6 (#63 / #65)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.feedback_store import FeedbackRecord, InMemoryFeedbackStore
from expert_work.protocol import AuditQuery, CurationCandidateRecord
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
def app(
    audit_store: InMemoryAuditLogStore,
    feedback_store: InMemoryFeedbackStore,
) -> Any:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    return create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        feedback_repo=feedback_store,
        jwt_verifier=build_test_jwt_verifier(),
    )


@pytest.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
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


@pytest.mark.asyncio
async def test_up_from_a_different_actor_does_not_mark_someone_elses_down_as_changed(
    client: AsyncClient, app: Any, feedback_store: InMemoryFeedbackStore
) -> None:
    """改票标记按 run 命中候选行,所以「上一票」必须是**调用者自己**的那一票。

    终端用户 👎 过这一轮(候选行已带 ``feedback_run_id``),员工随后在控制台给
    同一轮打 👍 —— 员工自己从没打过 👎,候选行不该被标成「后改为 👍」。少了
    ``previous`` 的 actor 谓词,员工这一票会读到别人的 👎 当作自己的上一票。
    """
    thread_id, run_id = uuid4(), uuid4()
    at = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    await feedback_store.insert(
        FeedbackRecord(
            tenant_id=_DEFAULT_TENANT,
            thread_id=thread_id,
            run_id=run_id,
            rating="down",
            actor_id="end-user-77",
        )
    )
    candidates = app.state.curation_candidate_store
    await candidates.upsert(
        CurationCandidateRecord(
            id=uuid4(),
            tenant_id=_DEFAULT_TENANT,
            agent_name="reporter",
            agent_version="1.0.0",
            thread_id=thread_id,
            trajectory_key=f"trajectories/{_DEFAULT_TENANT}/failed/2026/09/09/{thread_id}.jsonl",
            outcome="failed",
            signal="negative_feedback",
            feedback_rating="down",
            feedback_run_id=run_id,
            detected_at=at,
        )
    )

    resp = await client.post(
        f"/v1/sessions/{thread_id}/feedback",
        json={"rating": "up", "run_id": str(run_id)},
    )
    assert resp.status_code == 201, resp.text
    rows = await candidates.list_for_review(tenant_id=_DEFAULT_TENANT)
    assert len(rows) == 1 and rows[0].feedback_changed_at is None


@pytest.mark.asyncio
async def test_candidate_sync_failure_never_loses_the_vote(
    client: AsyncClient,
    feedback_store: InMemoryFeedbackStore,
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制台侧同一条不变式:进池炸了,员工那一票照样落库、照样 201。

    与对外端点是两条独立的写路径 —— 两边各留一条,改坏一边不会被另一边遮住。
    """

    async def _boom(**_kwargs: Any) -> str:
        raise RuntimeError("candidate sync is down")

    monkeypatch.setattr("control_plane.api.feedback.sync_candidate_for_feedback", _boom)
    thread_id, run_id = uuid4(), uuid4()

    resp = await client.post(
        f"/v1/sessions/{thread_id}/feedback",
        json={"rating": "down", "comment": "bad", "run_id": str(run_id)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["rating"] == "down"

    rows = await feedback_store.list_for_thread(thread_id=thread_id)
    assert len(rows) == 1
    assert rows[0].rating == "down" and rows[0].comment == "bad" and rows[0].run_id == run_id

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT, limit=100))
    entries = [e for e in page.entries if e.action.value == "feedback:create"]
    assert len(entries) == 1
    assert entries[0].details["candidate"] == "failed"


# ---------------------------------------------------------------------------
# GET /v1/sessions/{thread_id}/feedback — P-2 PR3 Task 12
# ---------------------------------------------------------------------------


async def _seed_thread(app: Any, thread_id: UUID, *, tenant_id: UUID = _DEFAULT_TENANT) -> None:
    await app.state.thread_meta_repo.create(
        thread_id=thread_id,
        tenant_id=tenant_id,
        created_by="seed",
        user_id=uuid4(),
        agent_name="alpha",
        agent_version="1.0.0",
    )


@pytest.mark.asyncio
async def test_get_feedback_lists_every_rating_on_the_thread_newest_first(
    client: AsyncClient, app: Any, feedback_store: InMemoryFeedbackStore
) -> None:
    """全员可见的是**这条会话的全部**反馈,不只是调用者自己那条。

    与对外 ``/items`` / ``/messages`` 的「只回显本人」语义**刻意不同**:控制台
    是运营审阅面,员工要看得到终端用户打的分。
    """
    thread_id, run_a, run_b = uuid4(), uuid4(), uuid4()
    await _seed_thread(app, thread_id)
    await feedback_store.upsert(
        FeedbackRecord(
            tenant_id=_DEFAULT_TENANT,
            thread_id=thread_id,
            run_id=run_a,
            rating="down",
            comment="太慢",
            item_id="p1",
            source="external",
            actor_id="ext-1",
        )
    )
    await feedback_store.upsert(
        FeedbackRecord(
            tenant_id=_DEFAULT_TENANT,
            thread_id=thread_id,
            run_id=run_b,
            rating="up",
            source="console",
            actor_id="emp-1",
        )
    )
    resp = await client.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["run_id"] for i in items] == [str(run_b), str(run_a)]
    assert items[1]["created_at"] is not None
    assert items[1] == {
        "id": items[1]["id"],
        "run_id": str(run_a),
        "rating": "down",
        "comment": "太慢",
        "item_id": "p1",
        "source": "external",
        "actor_id": "ext-1",
        "created_at": items[1]["created_at"],
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_get_feedback_does_not_leak_another_threads_rows(
    client: AsyncClient, app: Any, feedback_store: InMemoryFeedbackStore
) -> None:
    """同租户里另一条会话的反馈不能出现在这条会话的列表里(thread 谓词自证)。"""
    thread_id, other_thread = uuid4(), uuid4()
    await _seed_thread(app, thread_id)
    await _seed_thread(app, other_thread)
    await feedback_store.upsert(
        FeedbackRecord(
            tenant_id=_DEFAULT_TENANT,
            thread_id=other_thread,
            run_id=uuid4(),
            rating="down",
            comment="OTHER-THREAD",
            source="external",
            actor_id="ext-2",
        )
    )
    resp = await client.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert "OTHER-THREAD" not in resp.text


@pytest.mark.asyncio
async def test_get_feedback_is_readable_by_a_viewer_including_comments(
    feedback_store: InMemoryFeedbackStore, audit_store: InMemoryAuditLogStore
) -> None:
    """用户拍板:评论原文全员可见,与会话原文的门槛**有意不一致**。

    这条用例同时钉住两条轴 —— ``viewer-1`` 既**不是 admin**、也**不是这条会话的
    owner**(``_seed_thread`` 给的 ``user_id`` 是另一个 uuid):
    * 角色轴:``session:read`` 就够,不要求 ``write``;
    * 归属轴:没有 ``caller_owns_thread``(``GET /v1/sessions/{tid}`` 与
      ``.../messages`` 都挂着它,这条运营审阅面刻意不挂)。
    """
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    viewer_app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        feedback_repo=feedback_store,
        jwt_verifier=build_test_jwt_verifier(),
    )
    thread_id = uuid4()
    await _seed_thread(viewer_app, thread_id)
    await feedback_store.upsert(
        FeedbackRecord(
            tenant_id=_DEFAULT_TENANT,
            thread_id=thread_id,
            run_id=uuid4(),
            rating="down",
            comment="VISIBLE-TO-VIEWER",
            source="external",
            actor_id="ext-1",
        )
    )
    token = make_test_jwt(tenant_id=_DEFAULT_TENANT, subject="viewer-1", roles=("viewer",))
    async with AsyncClient(
        transport=ASGITransport(app=viewer_app),
        base_url="http://control-plane.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as viewer:
        resp = await viewer.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["comment"] == "VISIBLE-TO-VIEWER"


@pytest.mark.asyncio
async def test_get_feedback_404s_for_a_thread_of_another_tenant(
    client: AsyncClient, app: Any, feedback_store: InMemoryFeedbackStore
) -> None:
    """跨租户:thread 属于别人 → 404,且别人的评论原文一个字都不出现。"""
    other_tenant, thread_id = uuid4(), uuid4()
    await _seed_thread(app, thread_id, tenant_id=other_tenant)
    await feedback_store.upsert(
        FeedbackRecord(
            tenant_id=other_tenant,
            thread_id=thread_id,
            run_id=uuid4(),
            rating="down",
            comment="LEAK?",
            source="external",
            actor_id="ext-9",
        )
    )
    resp = await client.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 404, resp.text
    assert "LEAK?" not in resp.text


@pytest.mark.asyncio
async def test_get_feedback_404s_for_an_unknown_thread(client: AsyncClient) -> None:
    resp = await client.get(f"/v1/sessions/{uuid4()}/feedback")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_get_feedback_read_carries_its_own_tenant_predicate(
    client: AsyncClient, app: Any, feedback_store: InMemoryFeedbackStore
) -> None:
    """第二道门单独自证:thread **在**本租户(404 那道门放行),同 id 上挂着另一
    租户的反馈行 → 读行本身必须再滤一次租户。

    没有这一条,把 ``list_for_thread_scoped`` 换回无租户谓词的 ``list_for_thread``
    整套用例照样全绿 —— 因为跨租户那条被 404 先挡住了,两道门里只有一道被测到。
    运行期以 BYPASSRLS 角色连库,RLS 兜底是空的,这道谓词就是唯一的过滤。
    """
    other_tenant, thread_id = uuid4(), uuid4()
    await _seed_thread(app, thread_id)  # 本租户有这条会话 → 404 那道门放行
    await feedback_store.upsert(
        FeedbackRecord(
            tenant_id=other_tenant,
            thread_id=thread_id,
            run_id=uuid4(),
            rating="down",
            comment="OTHER-TENANT-ROW",
            source="external",
            actor_id="ext-9",
        )
    )
    resp = await client.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert "OTHER-TENANT-ROW" not in resp.text
