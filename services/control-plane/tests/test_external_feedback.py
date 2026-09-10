"""对外打分 —— ``POST /v1/agents/{agent_code}/runs/{run_id}/feedback``(P-2 PR1)。

夹具照 ``test_external_runs_cancel.py``:服务账号 key、直接往 ``RunStore`` 写 run 行。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.feedback_store import InMemoryFeedbackStore
from expert_work.protocol import AgentSpec, AuditQuery
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
)
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryRecord, TrajectoryRecorder
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        headers: dict[str, str],
        run_store: InMemoryRunStore,
        feedback: InMemoryFeedbackStore,
        audit_store: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store
        self.feedback = feedback
        self.audit_store = audit_store

    async def seed_agent(self, name: str = "support-bot") -> None:
        spec = _spec().model_copy(deep=True)
        spec.metadata.name = name
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=spec, spec_sha256="a" * 64, created_by="seed"
        )

    async def bind_session(self, user_id: str, agent: str = "support-bot") -> UUID:
        bound = await self.client.post(
            f"/v1/agents/{agent}/sessions", json={"user_id": user_id}, headers=self.headers
        )
        assert bound.status_code == 201, bound.text
        return UUID(bound.json()["data"]["session_id"])

    async def end_user_id(self, user_id: str) -> UUID:
        row = await self.app.state.tenant_user_repo.resolve(
            tenant_id=self.tenant_id, subject_type="user", subject_id=f"ext:{user_id}"
        )
        return row.id  # type: ignore[no-any-return]

    async def seed_run(self, thread_id: UUID, user_id: str) -> UUID:
        run_id = uuid4()
        await self.run_store.create(
            RunInfo(
                run_id=run_id,
                tenant_id=self.tenant_id,
                thread_id=thread_id,
                user_id=await self.end_user_id(user_id),
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=_NOW,
                updated_at=_NOW,
                finished_at=_NOW,
            )
        )
        return run_id

    async def rate(self, run_id: UUID, body: dict[str, Any], agent: str = "support-bot") -> Any:
        return await self.client.post(
            f"/v1/agents/{agent}/runs/{run_id}/feedback", json=body, headers=self.headers
        )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    feedback = InMemoryFeedbackStore()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        feedback_repo=feedback,
    )
    tenant_id = uuid4()
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, run_store, feedback, audit_store)


@pytest.mark.asyncio
async def test_down_then_up_is_one_row_last_wins(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")

    first = await ctx.rate(
        run_id, {"user_id": "cust-77", "rating": "down", "comment": "太慢", "item_id": "p2"}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "success": True,
        "data": {"run_id": str(run_id), "rating": "down", "updated": False},
        "error": None,
    }

    second = await ctx.rate(run_id, {"user_id": "cust-77", "rating": "up"})
    assert second.status_code == 200, second.text
    assert second.json()["data"] == {"run_id": str(run_id), "rating": "up", "updated": True}

    rows = await ctx.feedback.list_for_thread_scoped(tenant_id=ctx.tenant_id, thread_id=thread)
    assert len(rows) == 1
    assert rows[0].rating == "up"
    assert rows[0].comment is None and rows[0].item_id is None
    assert rows[0].run_id == run_id
    assert rows[0].source == "external"
    assert rows[0].actor_id == str(await ctx.end_user_id("cust-77"))
    assert rows[0].updated_at is not None


@pytest.mark.asyncio
async def test_other_user_and_other_agent_are_404_envelope(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    await ctx.seed_agent("other-bot")
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")

    other_user = await ctx.rate(run_id, {"user_id": "cust-99", "rating": "down"})
    assert other_user.status_code == 404, other_user.text
    assert other_user.json() == {
        "success": False,
        "data": None,
        "error": {"code": "RUN_NOT_FOUND", "message": "run not found"},
    }
    other_agent = await ctx.rate(
        run_id, {"user_id": "cust-77", "rating": "down"}, agent="other-bot"
    )
    assert other_agent.status_code == 404
    assert other_agent.json()["error"]["code"] == "RUN_NOT_FOUND"
    # 404 不铸造 tenant_user 行(load_owned_run 是 mint=False)。
    users = await ctx.app.state.tenant_user_repo.list_by_tenant(ctx.tenant_id, subject_type="user")
    assert {u.subject_id for u in users} == {"ext:cust-77"}
    assert (
        await ctx.feedback.list_for_thread_scoped(tenant_id=ctx.tenant_id, thread_id=thread) == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_body",
    [
        {"user_id": "cust-77"},  # missing rating
        {"user_id": "cust-77", "rating": "sideways"},
        {"user_id": "cust-77", "rating": "up", "comment": "x\x00y"},  # NUL
        {"user_id": "cust-77", "rating": "up", "item_id": "a\x00b"},  # NUL
        {"user_id": "cust-77", "rating": "up", "comment": "c" * 4001},
        {"user_id": "cust-77", "rating": "up", "extra": 1},
    ],
)
async def test_bad_bodies_are_422_envelope(ctx: _Ctx, bad_body: dict[str, Any]) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    resp = await ctx.rate(run_id, bad_body)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False and body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert "detail" not in body


@pytest.mark.asyncio
async def test_audit_row_carries_run_and_rating_but_never_the_comment(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    resp = await ctx.rate(
        run_id, {"user_id": "cust-77", "rating": "down", "comment": "SECRET-PROSE"}
    )
    assert resp.status_code == 200
    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=100))
    rows = [e for e in page.entries if e.action.value == "feedback:create"]
    assert len(rows) == 1
    assert rows[0].details["run_id"] == str(run_id)
    assert rows[0].details["rating"] == "down"
    assert rows[0].details["source"] == "external"
    assert rows[0].on_behalf_of == str(await ctx.end_user_id("cust-77"))
    assert "SECRET-PROSE" not in str(rows[0].details)


async def _land_trajectory(ctx: _Ctx, thread: UUID, run_id: UUID) -> None:
    object_store = InMemoryObjectStore()
    ctx.app.state.object_store = object_store
    await TrajectoryRecorder(object_store=object_store).record(
        TrajectoryRecord(
            thread_id=thread,
            tenant_id=ctx.tenant_id,
            outcome="success",
            messages=[HumanMessage(content="hi"), AIMessage(content="bye")],
            run_id=run_id,
            finished_at=_NOW,
        )
    )


@pytest.mark.asyncio
async def test_down_lands_a_candidate_in_the_same_request(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    await _land_trajectory(ctx, thread, run_id)
    resp = await ctx.rate(run_id, {"user_id": "cust-77", "rating": "down", "comment": "太慢"})
    assert resp.status_code == 200, resp.text
    rows = await ctx.app.state.curation_candidate_store.list_for_review(tenant_id=ctx.tenant_id)
    assert len(rows) == 1
    assert rows[0].signal == "negative_feedback" and rows[0].feedback_run_id == run_id
    assert rows[0].feedback_comment == "太慢"
    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=100))
    assert [
        e.details["candidate"] for e in page.entries if e.action.value == "feedback:create"
    ] == ["inserted"]


@pytest.mark.asyncio
async def test_up_never_lands_a_candidate_and_change_is_marked(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    await _land_trajectory(ctx, thread, run_id)
    assert (await ctx.rate(run_id, {"user_id": "cust-77", "rating": "up"})).status_code == 200
    candidates = ctx.app.state.curation_candidate_store
    assert await candidates.list_for_review(tenant_id=ctx.tenant_id) == []
    assert (await ctx.rate(run_id, {"user_id": "cust-77", "rating": "down"})).status_code == 200
    assert (await ctx.rate(run_id, {"user_id": "cust-77", "rating": "up"})).status_code == 200
    rows = await candidates.list_for_review(tenant_id=ctx.tenant_id)
    assert len(rows) == 1 and rows[0].feedback_changed_at is not None
