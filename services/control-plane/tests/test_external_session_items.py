"""会话历史条目接口 —— ``GET /v1/agents/{agent_code}/sessions/{session_id}/items``。

对话条目 program PR2。夹具照搬 ``test_external_sessions.py``:同一个外部平面、
同一套服务账号 JWT,区别只在被测端点。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.common.spotlight import spotlight_untrusted
from expert_work.persistence.approval import InMemoryApprovalStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.protocol.approval import ApprovalRecord, ApprovalStatus
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
    make_event_record,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_BASE = datetime(2026, 8, 25, 9, 0, 0, tzinfo=UTC)


class _SeedState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


async def _seed_thread_messages(
    checkpointer: InMemorySaver, thread_id: str, messages: list[BaseMessage]
) -> None:
    """写一份带 ``messages`` 的检查点(模拟真实 run 留下的那份)。"""
    graph = StateGraph(_SeedState)
    graph.add_node("n", lambda _state: {"messages": []})
    graph.add_edge(START, "n")
    seeded = graph.compile(checkpointer=checkpointer)
    await seeded.ainvoke(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
    )


def _stamp(run_id: UUID, at: datetime) -> dict[str, str]:
    return {STAMP_RUN_ID: str(run_id), STAMP_CREATED_AT: at.isoformat()}


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
        event_store: InMemoryRunEventStore,
        approvals: InMemoryApprovalStore,
        checkpointer: InMemorySaver,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store
        self.event_store = event_store
        self.approvals = approvals
        self.checkpointer = checkpointer
        #: 第一轮 run 的真实 ``created_at`` —— 后续轮次的时刻都相对它算。
        self.origin: datetime = _BASE

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def open_session(
        self, user_id: str = "u-123", *, finish: bool = True
    ) -> tuple[UUID, UUID]:
        """建一段会话,返回 ``(session_id, 第一轮 run_id)``。

        ``mode="queue"`` 建出来的 run 是 ``queued`` —— 那是「进行中」,会被条目
        接口排除。除非测的就是进行中那一轮,默认把它推到终态,并且把耗时钉成
        5 秒(``finished_at`` 相对该行真实的 ``created_at`` 算,不然 duration
        会是「现在减去 2026 年某个固定时刻」)。
        """
        started = await self.client.post(
            "/v1/agents/support-bot/runs",
            json={"user_id": user_id, "input": "hi", "mode": "queue"},
            headers=self.headers,
        )
        assert started.status_code == 202, started.text
        data = started.json()["data"]
        session_id, run_id = UUID(data["thread_id"]), UUID(data["run_id"])
        row = await self.run_store.get(run_id=run_id, tenant_id=self.tenant_id)
        assert row is not None
        # 这一轮的时刻是「此刻」,不是本文件里那个固定的 ``_BASE``。后续轮次
        # 必须相对它排,否则轮次顺序会随运行时间翻转。
        self.origin = row.created_at
        if finish:
            await self.run_store.set_status(
                run_id=run_id,
                tenant_id=self.tenant_id,
                status=RunStatus.SUCCESS,
                updated_at=row.created_at,
                finished_at=row.created_at + timedelta(seconds=5),
            )
        return session_id, run_id

    async def add_run(
        self,
        session_id: UUID,
        *,
        created_at: datetime,
        status: RunStatus = RunStatus.SUCCESS,
        finished_at: datetime | None = None,
        error: str | None = None,
    ) -> UUID:
        """在同一段会话上再加一轮 —— 直接写 store,时刻才可控。"""
        run_id = uuid4()
        await self.run_store.create(
            RunInfo(
                run_id=run_id,
                tenant_id=self.tenant_id,
                thread_id=session_id,
                user_id=None,
                status=status,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=error,
                created_at=created_at,
                updated_at=created_at,
                finished_at=finished_at,
            )
        )
        return run_id

    async def items(self, session_id: UUID, **params: Any) -> Any:
        query: dict[str, Any] = {"user_id": "u-123", **params}
        resp = await self.client.get(
            f"/v1/agents/support-bot/sessions/{session_id}/items",
            params=query,
            headers=self.headers,
        )
        return resp


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    approvals = InMemoryApprovalStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        approval_repo=approvals,
    )
    checkpointer = InMemorySaver()
    app.state.agent_runtime.durable_checkpointer = checkpointer
    tenant_id = uuid4()
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(
            client,
            app,
            tenant_id,
            headers,
            run_store,
            run_event_store,
            approvals,
            checkpointer,
        )


@pytest.mark.asyncio
async def test_items_404_for_another_user(ctx: _Ctx) -> None:
    """别人的会话是 404,不是空列表 —— 响应不能携带存在性信息。"""
    await ctx.seed_agent()
    session_id, _ = await ctx.open_session()

    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/items",
        params={"user_id": "someone-else"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert resp.json()["data"] is None

    # 同一段会话对它自己的主人是 200 —— 否则上面的 404 可能只是「这个端点
    # 对谁都 404」。
    ok = await ctx.items(session_id)
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_items_404_for_another_agent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    session_id, _ = await ctx.open_session()

    resp = await ctx.client.get(
        f"/v1/agents/other-bot/sessions/{session_id}/items",
        params={"user_id": "u-123"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_items_requires_user_id(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    session_id, _ = await ctx.open_session()

    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/items", headers=ctx.headers
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_items_renders_one_turn_end_to_end(ctx: _Ctx) -> None:
    """一轮完整对话:用户消息 → 助手文本 → 工具调用 → 工具结果。

    工具结果的正文必须是**还原后**的文本(内部的防注入包装不出现在对外
    响应里),``created_at`` 恒为 ``null``(工具消息不盖戳),``run_id`` 从
    所属轮继承。
    """
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [
            HumanMessage(content="查一下天气", additional_kwargs=_stamp(run_id, _BASE)),
            AIMessage(
                content="这就去查",
                additional_kwargs=_stamp(run_id, _BASE + timedelta(seconds=1)),
                tool_calls=[
                    {"id": "call-1", "name": "search", "args": {"q": "天气"}, "type": "tool_call"}
                ],
            ),
            ToolMessage(
                content=spotlight_untrusted("今天晴", nonce="n0nce"),
                tool_call_id="call-1",
                name="search",
                status="success",
                additional_kwargs={"duration_ms": 42},
            ),
            AIMessage(
                content="今天晴。",
                additional_kwargs=_stamp(run_id, _BASE + timedelta(seconds=3)),
            ),
        ],
    )

    resp = await ctx.items(session_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]

    assert [i["type"] for i in body["items"]] == [
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
    ]
    assert [i["run_id"] for i in body["items"]] == [str(run_id)] * 5
    # 同一响应内 id 唯一 —— 客户端拿它当 key。
    assert len({i["id"] for i in body["items"]}) == 5

    user, first_ai, call, result, final = body["items"]
    assert user["content"] == "查一下天气"
    assert user["attachments"] == []
    assert user["created_at"] == _BASE.isoformat()
    assert first_ai["channel"] == "commentary"
    assert final["channel"] == "final"
    assert call["call_id"] == "call-1"
    assert call["name"] == "search"
    assert call["args"] == {"q": "天气"}
    # 还原后的正文里既没有围栏也没有 datamark 标记。
    assert "今天" in result["content"]
    assert "expert-work-untrusted" not in result["content"]
    assert "▁" not in result["content"]
    assert result["status"] == "success"
    assert result["duration_ms"] == 42
    # 工具消息不盖戳:时刻只能缺席,不能编。
    assert result["created_at"] is None

    assert body["runs"] == [
        {
            "run_id": str(run_id),
            "status": "success",
            "created_at": body["runs"][0]["created_at"],
            "duration_ms": 5000,
            "error": None,
        }
    ]
    assert body["runs"][0]["created_at"] is not None
    assert body["has_more"] is False
    assert body["first_run_id"] == str(run_id)
    assert body["active_run_id"] is None


@pytest.mark.asyncio
async def test_items_skips_messages_without_a_run_stamp(ctx: _Ctx) -> None:
    """没盖 ``run_id`` 戳的老消息归不到轮,不返回。

    两个位置都要覆盖:盖戳消息**之前**的老消息(归不到任何轮),以及盖戳消息
    **之后**的老消息 —— 后者才是真正会咬人的那个,「顺延上一条的归属」会把它
    塞进上一轮。而不盖戳的工具结果恰恰必须顺延,两条规则必须分得开。
    """
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [
            HumanMessage(content="上线前的老消息"),
            AIMessage(content="上线前的老回答"),
            HumanMessage(content="新问题", additional_kwargs=_stamp(run_id, _BASE)),
            AIMessage(
                content="新回答",
                additional_kwargs=_stamp(run_id, _BASE + timedelta(seconds=1)),
                tool_calls=[{"id": "c1", "name": "t", "args": {}, "type": "tool_call"}],
            ),
            ToolMessage(content="结果", tool_call_id="c1", name="t", status="success"),
            HumanMessage(content="夹在中间的老消息"),
            AIMessage(content="夹在中间的老回答"),
        ],
    )

    body = (await ctx.items(session_id)).json()["data"]
    contents = [i.get("content") for i in body["items"]]
    # 盖了戳的三条 + 顺延的工具结果,一条不多一条不少。
    assert [i["type"] for i in body["items"]] == [
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
    ]
    assert "新问题" in contents
    assert "新回答" in contents
    assert "上线前的老消息" not in contents
    assert "上线前的老回答" not in contents
    assert "夹在中间的老消息" not in contents
    assert "夹在中间的老回答" not in contents
    # 工具结果继承所属轮。
    assert body["items"][-1]["run_id"] == str(run_id)


@pytest.mark.asyncio
async def test_items_excludes_the_active_run(ctx: _Ctx) -> None:
    """正在跑的那一轮只报 ``active_run_id``,内容留给实时接口。"""
    await ctx.seed_agent()
    session_id, done_run = await ctx.open_session()
    live_run = await ctx.add_run(
        session_id, created_at=ctx.origin + timedelta(minutes=1), status=RunStatus.RUNNING
    )
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [
            HumanMessage(content="第一轮", additional_kwargs=_stamp(done_run, _BASE)),
            AIMessage(content="第一轮答", additional_kwargs=_stamp(done_run, _BASE)),
            HumanMessage(
                content="第二轮", additional_kwargs=_stamp(live_run, _BASE + timedelta(minutes=1))
            ),
        ],
    )

    body = (await ctx.items(session_id)).json()["data"]
    assert body["active_run_id"] == str(live_run)
    # 已结束那轮的内容在;进行中那轮一条都不在。
    assert [i["content"] for i in body["items"]] == ["第一轮", "第一轮答"]
    assert {i["run_id"] for i in body["items"]} == {str(done_run)}
    assert [r["run_id"] for r in body["runs"]] == [str(done_run)]


@pytest.mark.asyncio
async def test_items_pagination_walks_back_without_gaps_or_duplicates(ctx: _Ctx) -> None:
    """按轮翻页:两页合起来正好是全部三轮,不重不漏。"""
    await ctx.seed_agent()
    session_id, run1 = await ctx.open_session()
    run2 = await ctx.add_run(session_id, created_at=ctx.origin + timedelta(minutes=1))
    run3 = await ctx.add_run(session_id, created_at=ctx.origin + timedelta(minutes=2))
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [
            HumanMessage(content="q1", additional_kwargs=_stamp(run1, _BASE)),
            HumanMessage(
                content="q2", additional_kwargs=_stamp(run2, _BASE + timedelta(minutes=1))
            ),
            HumanMessage(
                content="q3", additional_kwargs=_stamp(run3, _BASE + timedelta(minutes=2))
            ),
        ],
    )

    page1 = (await ctx.items(session_id, limit=2)).json()["data"]
    assert [r["run_id"] for r in page1["runs"]] == [str(run2), str(run3)]
    assert [i["content"] for i in page1["items"]] == ["q2", "q3"]
    assert page1["has_more"] is True
    assert page1["first_run_id"] == str(run2)

    page2 = (await ctx.items(session_id, limit=2, before=page1["first_run_id"])).json()["data"]
    assert [r["run_id"] for r in page2["runs"]] == [str(run1)]
    assert [i["content"] for i in page2["items"]] == ["q1"]
    assert page2["has_more"] is False

    # 两页合起来 = 全部三轮,顺序正确、没有重复。
    assert [r["run_id"] for r in page2["runs"]] + [r["run_id"] for r in page1["runs"]] == [
        str(run1),
        str(run2),
        str(run3),
    ]


@pytest.mark.asyncio
async def test_items_before_must_belong_to_this_session(ctx: _Ctx) -> None:
    """游标是别的会话的 run 时 404,不是「悄悄给最近几轮」。"""
    await ctx.seed_agent()
    session_id, _ = await ctx.open_session()
    other_session, other_run = await ctx.open_session(user_id="u-other")

    resp = await ctx.items(session_id, before=str(other_run))
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert other_session != session_id


@pytest.mark.asyncio
async def test_items_carries_plan_error_and_approval_frames(ctx: _Ctx) -> None:
    """``plan`` / ``approval`` / ``error`` 三种辅助信号变成条目,时刻取自
    服务端记录这些事件的时间(它们的内容里没有时刻)。"""
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [
            HumanMessage(content="做个计划", additional_kwargs=_stamp(run_id, _BASE)),
            AIMessage(content="好的", additional_kwargs=_stamp(run_id, _BASE)),
        ],
    )
    plan_at = _BASE + timedelta(seconds=2)
    await ctx.event_store.append_batch(
        [
            make_event_record(run_id=run_id, seq=0, event_name="metadata", data={"x": 1}),
            make_event_record(
                run_id=run_id,
                seq=1,
                event_name="plan",
                data={"goal": "旧目标", "steps": []},
                created_at_ms=int((plan_at - timedelta(seconds=1)).timestamp() * 1000),
            ),
            make_event_record(
                run_id=run_id,
                seq=2,
                event_name="plan",
                data={"goal": "查天气", "steps": [{"title": "查", "status": "done"}]},
                created_at_ms=int(plan_at.timestamp() * 1000),
            ),
            make_event_record(
                run_id=run_id,
                seq=3,
                event_name="approval",
                data={
                    "request_id": "approval:abc",
                    "node": "tools",
                    "reason_kind": "policy_gate",
                    "action_summary": "approval-gated tool 'bash'",
                    "proposed_args": {"cmd": "ls"},
                    "requested_at": "2026-08-25T09:00:05+00:00",
                    "timeout_at": "2026-08-26T09:00:05+00:00",
                },
            ),
            make_event_record(
                run_id=run_id,
                seq=4,
                event_name="error",
                data={"message": "撞了步数上限", "name": "MaxStepsExceededError"},
            ),
        ]
    )

    body = (await ctx.items(session_id)).json()["data"]
    by_type = {i["type"]: i for i in body["items"]}
    assert [i["type"] for i in body["items"]] == [
        "user_message",
        "plan",
        "assistant_message",
        "approval",
        "error",
    ]
    # 一轮里改过几次计划只留最后一份。
    assert by_type["plan"]["goal"] == "查天气"
    assert by_type["plan"]["steps"] == [{"title": "查", "status": "done"}]
    assert by_type["plan"]["created_at"] == plan_at.isoformat()
    assert by_type["approval"]["request_id"] == "approval:abc"
    assert by_type["approval"]["reason_kind"] == "policy_gate"
    assert by_type["approval"]["timeout_at"] == "2026-08-26T09:00:05+00:00"
    # 没有裁定结果时 ``decision`` 缺席,不是 null 也不是编一个。
    assert "decision" not in by_type["approval"]
    assert by_type["error"]["message"] == "撞了步数上限"
    assert by_type["error"]["name"] == "MaxStepsExceededError"
    assert by_type["error"]["created_at"] is not None


@pytest.mark.asyncio
async def test_items_backfills_the_approval_decision(ctx: _Ctx) -> None:
    """人的裁定不在任何事件里,只在审批记录上 —— 条目要把它带回来。"""
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [HumanMessage(content="删个文件", additional_kwargs=_stamp(run_id, _BASE))],
    )
    await ctx.event_store.append(
        make_event_record(
            run_id=run_id,
            seq=0,
            event_name="approval",
            data={
                "request_id": "approval:abc",
                "node": "tools",
                "reason_kind": "policy_gate",
                "action_summary": "approval-gated tool 'bash'",
                "proposed_args": {},
                "requested_at": "2026-08-25T09:00:05+00:00",
                "timeout_at": "2026-08-26T09:00:05+00:00",
            },
        )
    )
    await ctx.approvals.create(
        ApprovalRecord(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            thread_id=session_id,
            request_id="approval:abc",
            node="tools",
            reason_kind="policy_gate",
            action_summary="approval-gated tool 'bash'",
            requested_at=_BASE,
            timeout_at=_BASE + timedelta(days=1),
        )
    )

    pending = (await ctx.items(session_id)).json()["data"]
    approval = next(i for i in pending["items"] if i["type"] == "approval")
    assert "decision" not in approval

    decided = await ctx.approvals.mark_decided(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=ApprovalStatus.REJECTED,
        decided_by="u-123",
        decided_at=_BASE + timedelta(minutes=1),
    )
    assert decided is True

    body = (await ctx.items(session_id)).json()["data"]
    approval = next(i for i in body["items"] if i["type"] == "approval")
    assert approval["decision"] == "rejected"


@pytest.mark.asyncio
async def test_items_returns_run_level_info_when_the_checkpoint_is_gone(ctx: _Ctx) -> None:
    """上下文压缩丢弃过中段的会话:那几轮消息没了,返回不完整而不是报错。"""
    await ctx.seed_agent()
    session_id, run1 = await ctx.open_session()
    await ctx.run_store.set_status(
        run_id=run1,
        tenant_id=ctx.tenant_id,
        status=RunStatus.ERROR,
        updated_at=ctx.origin,
        error="boom",
        finished_at=ctx.origin + timedelta(seconds=3),
    )
    run2 = await ctx.add_run(
        session_id,
        created_at=ctx.origin + timedelta(minutes=1),
        finished_at=ctx.origin + timedelta(minutes=1, seconds=2),
    )
    # 只有第二轮的消息还在检查点里。
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [HumanMessage(content="还在的那条", additional_kwargs=_stamp(run2, _BASE))],
    )

    resp = await ctx.items(session_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert [i["content"] for i in body["items"]] == ["还在的那条"]
    # 两轮的轮级信息都还在,失败原因与耗时照给。
    assert [(r["run_id"], r["error"], r["duration_ms"]) for r in body["runs"]] == [
        (str(run1), "boom", 3000),
        (str(run2), None, 2000),
    ]


def _worker_frame(
    kind: str,
    *,
    worker_id: str,
    wseq: int,
    data: dict[str, Any],
    parent_worker_id: str | None = None,
    parent_tool_call_id: str | None = None,
    label: str = "调研员",
    agent_ref: str = "dynamic:general",
    depth: int = 1,
) -> dict[str, Any]:
    """一帧 ``worker`` —— 信封字段与 ``_worker_events.build_*_frame`` 逐一同名。"""
    return {
        "worker_id": worker_id,
        "parent_worker_id": parent_worker_id,
        "parent_tool_call_id": parent_tool_call_id,
        "label": label,
        "agent_ref": agent_ref,
        "depth": depth,
        "kind": kind,
        "wseq": wseq,
        "data": data,
    }


async def _seed_one_tool_call_turn(ctx: _Ctx, session_id: UUID, run_id: UUID) -> None:
    """一轮:用户消息 → 一条带两次工具调用的助手消息。

    两次调用是有意的 —— 只种一次的话「没派生子任务的工具调用不带 ``worker``
    键」那条断言在回填整体失灵时同样成立。
    """
    await _seed_thread_messages(
        ctx.checkpointer,
        str(session_id),
        [
            HumanMessage(content="查一下夜间排班", additional_kwargs=_stamp(run_id, _BASE)),
            AIMessage(
                content="这就去",
                additional_kwargs=_stamp(run_id, _BASE + timedelta(seconds=1)),
                tool_calls=[
                    {
                        "id": "call-worker",
                        "name": "spawn_worker",
                        "args": {"task": "查排班"},
                        "type": "tool_call",
                    },
                    {"id": "call-plain", "name": "search", "args": {}, "type": "tool_call"},
                ],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_items_backfills_the_worker_tree_onto_its_tool_call(ctx: _Ctx) -> None:
    """深度 1 的子任务按 ``parent_tool_call_id`` 挂到发起它的工具调用上。

    那个值就是 ``AIMessage.tool_calls[].id``,所以这一条不含任何猜测式配对。
    """
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    await _seed_one_tool_call_turn(ctx, session_id, run_id)
    await ctx.event_store.append_batch(
        [
            make_event_record(
                run_id=run_id,
                seq=0,
                event_name="worker",
                data=_worker_frame(
                    "start",
                    worker_id="w-1",
                    wseq=0,
                    parent_tool_call_id="call-worker",
                    data={"task_excerpt": "查排班", "role": "排班助手", "max_steps": 8},
                ),
            ),
            make_event_record(
                run_id=run_id,
                seq=1,
                event_name="worker",
                data=_worker_frame(
                    "update",
                    worker_id="w-1",
                    wseq=1,
                    parent_tool_call_id="call-worker",
                    data={
                        "node": "agent",
                        "_duration_ms": 120,
                        "step_count": 1,
                        "messages": [{"type": "ai", "content_excerpt": "在查了"}],
                    },
                ),
            ),
            make_event_record(
                run_id=run_id,
                seq=2,
                event_name="worker",
                data=_worker_frame(
                    "end",
                    worker_id="w-1",
                    wseq=2,
                    parent_tool_call_id="call-worker",
                    data={
                        "outcome": "success",
                        "iteration_used": 1,
                        "llm_call_count": 3,
                        "wall_clock_ms": 420,
                    },
                ),
            ),
        ]
    )

    body = (await ctx.items(session_id)).json()["data"]
    calls = {i["call_id"]: i for i in body["items"] if i["type"] == "tool_call"}
    assert set(calls) == {"call-worker", "call-plain"}

    worker = calls["call-worker"]["worker"]
    assert worker["worker_id"] == "w-1"
    assert worker["label"] == "调研员"
    assert worker["agent_ref"] == "dynamic:general"
    assert worker["depth"] == 1
    assert worker["task_excerpt"] == "查排班"
    assert worker["role"] == "排班助手"
    assert worker["max_steps"] == 8
    assert worker["status"] == "success"
    assert worker["summary"] == {
        "iteration_used": 1,
        "llm_call_count": 3,
        "wall_clock_ms": 420,
    }
    assert worker["steps"] == [
        {
            "wseq": 1,
            "node": "agent",
            "step_count": 1,
            "duration_ms": 120,
            "messages": [{"type": "ai", "content_excerpt": "在查了"}],
        }
    ]
    assert worker["children"] == []
    # 拼树用的两个父指引不进条目 —— 嵌套关系本身已经表达了同一件事。
    assert "parent_worker_id" not in worker
    assert "parent_tool_call_id" not in worker
    # 没派生子任务的那次调用整个键缺席(上面几条证明这不是「回填全失灵」)。
    assert "worker" not in calls["call-plain"]


@pytest.mark.asyncio
async def test_items_hangs_deeper_workers_by_parent_worker_id(ctx: _Ctx) -> None:
    """孙子任务按 ``parent_worker_id`` 挂树,不按 ``parent_tool_call_id``。

    孙子任务的 ``parent_tool_call_id`` 指向子 run **内部**那次工具调用,那个 id
    在父 run 的消息里根本不存在 —— 照它挂等于整棵挂丢。
    """
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    await _seed_one_tool_call_turn(ctx, session_id, run_id)
    await ctx.event_store.append_batch(
        [
            make_event_record(
                run_id=run_id,
                seq=0,
                event_name="worker",
                data=_worker_frame(
                    "start",
                    worker_id="w-1",
                    wseq=0,
                    parent_tool_call_id="call-worker",
                    data={"task_excerpt": "查排班", "role": None, "max_steps": 8},
                ),
            ),
            make_event_record(
                run_id=run_id,
                seq=1,
                event_name="worker",
                data=_worker_frame(
                    "start",
                    worker_id="w-2",
                    wseq=0,
                    parent_worker_id="w-1",
                    # 子 run 内部的 tool_call id —— 父 run 的消息里没有这个 id。
                    parent_tool_call_id="inner-call",
                    depth=2,
                    label="核对员",
                    data={"task_excerpt": "核对一遍", "role": None, "max_steps": 4},
                ),
            ),
            make_event_record(
                run_id=run_id,
                seq=2,
                event_name="worker",
                data=_worker_frame(
                    "end",
                    worker_id="w-2",
                    wseq=1,
                    parent_worker_id="w-1",
                    parent_tool_call_id="inner-call",
                    depth=2,
                    label="核对员",
                    data={
                        "outcome": "max_steps",
                        "iteration_used": 4,
                        "llm_call_count": 4,
                        "wall_clock_ms": 900,
                    },
                ),
            ),
        ]
    )

    body = (await ctx.items(session_id)).json()["data"]
    calls = {i["call_id"]: i for i in body["items"] if i["type"] == "tool_call"}
    # 根先立住 —— 下面「孙子任务没挂成根」才不是空集合上的恒真。
    root = calls["call-worker"]["worker"]
    assert root["worker_id"] == "w-1"
    # 孙子任务挂进根的 children,状态与摘要照给。
    assert [c["worker_id"] for c in root["children"]] == ["w-2"]
    assert root["children"][0]["depth"] == 2
    assert root["children"][0]["label"] == "核对员"
    assert root["children"][0]["status"] == "max_steps"
    assert root["children"][0]["task_excerpt"] == "核对一遍"
    # 子 run 内部那个 id 没有变成任何一次工具调用的挂载键。
    assert "inner-call" not in calls
    assert all(i["worker"]["worker_id"] != "w-2" for i in calls.values() if "worker" in i)
    # 根还没收到 ``end``,停在 ``running`` —— 不编一个结局。
    assert root["status"] == "running"
    assert root["summary"] is None


@pytest.mark.asyncio
async def test_worker_frames_do_not_crowd_out_the_plan_frame(ctx: _Ctx) -> None:
    """子任务帧单列一次查询 —— 否则它们会把 ``plan`` 挤出这一页。

    ``RunEventStore.list`` 的 ``limit`` 在名字过滤**之后**截断,上限 500。把
    ``worker`` 并进 ``plan`` / ``approval`` / ``error`` 那次查询,一轮里排在
    500 条子任务帧后面的计划就再也读不到了。
    """
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    await _seed_one_tool_call_turn(ctx, session_id, run_id)
    await ctx.event_store.append_batch(
        [
            make_event_record(
                run_id=run_id,
                seq=seq,
                event_name="worker",
                data=_worker_frame(
                    "update",
                    worker_id="w-1",
                    wseq=seq,
                    parent_tool_call_id="call-worker",
                    data={"node": "agent", "_duration_ms": 1, "messages": []},
                ),
            )
            for seq in range(500)
        ]
        + [
            make_event_record(
                run_id=run_id,
                seq=500,
                event_name="plan",
                data={"goal": "排到最后也要读得到", "steps": []},
            )
        ]
    )

    body = (await ctx.items(session_id)).json()["data"]
    assert [i["goal"] for i in body["items"] if i["type"] == "plan"] == ["排到最后也要读得到"]
    # 子任务这一路同样没被计划挤掉。
    calls = {i["call_id"]: i for i in body["items"] if i["type"] == "tool_call"}
    assert calls["call-worker"]["worker"]["worker_id"] == "w-1"
