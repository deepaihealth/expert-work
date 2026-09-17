"""新一轮作废上一轮的待审批 —— 对外端点 + 出队 + 裁定咽喉(班车 2 安全修复)。

缺陷复现形态:会话 T 里 run A 停在门控工具的审批上(A PAUSED、审批 PENDING),
调用方在 T 上直接发起 run B。修复前:B 的门控调用不经审批直接执行,B 带着 A 的
审批请求以 PAUSED 收场;事后批准 A,续跑的是已经属于 B 的检查点。

拍板语义:新一轮作废上一轮的待审批(不 409 调用方)—— A 收成 INTERRUPTED,
A 的审批不可再裁定,B 照常跑、审批门完整生效。

栈:真 ReAct 图 + 脚本化模型 + 一个门控工具(``lookup``)+ 内存 store,
全程走对外 ``POST /v1/agents/{code}/runs`` 与 ``:decide``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.api.runs import build_run_graph_input, replay_graph_input
from control_plane.app import create_app
from control_plane.approval_timeout_sweep import ApprovalTimeoutSweep
from control_plane.approval_void import (
    VOID_AUDIT_REASON,
    VOIDED_BY,
    repair_turn_tail,
    void_pending_approvals,
)
from control_plane.audit import build_default_audit_logger
from control_plane.run_queue_worker import RunQueueWorker
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.persistence import InMemoryApprovalStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import (
    AgentSpec,
    AgentSpecStatus,
    ApprovalRecord,
    ApprovalStatus,
    AuditEntry,
    AuditQuery,
)
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunManager,
    RunStatus,
)
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import (
    APPROVAL_TURN_RESET,
    AgentFactoryError,
    BuiltAgent,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
    sanitize_dangling_tool_calls,
)
from orchestrator.approval_turn import UNRUN_TOOL_CALL_CONTENT, VOIDED_APPROVAL_CONTENT
from orchestrator.sse import _BACKGROUND_PERSIST_WRITERS
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_AGENT = "support-bot"
_USER = "end-user-1"
_GATED = "lookup"

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": _AGENT, "version": "1.0.0", "tenant": "acme"},
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


def _gated(key: str) -> AIMessage:
    call = {"name": _GATED, "args": {"key": key}, "id": f"tc-{key}", "type": "tool_call"}
    return AIMessage(content="", tool_calls=[call])


@dataclass
class _Llm:
    """按脚本逐条回;脚本用完回一句收尾。记下每次调用看到的 prompt。"""

    script: list[AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        return self.script.pop(0) if self.script else AIMessage(content="done")


@dataclass
class _Lookup:
    """门控工具:记下每一次**真正执行**时的参数。"""

    seen: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=_GATED, description="look something up")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.seen.append(dict(args))
        return ToolResult(content="looked up")


_Hook = Callable[[], Awaitable[None]]


class _HookedApprovals(InMemoryApprovalStore):
    """``mark_decided`` 按状态挂一次性钩子,在真正 CAS 之前跑 —— 用来摆竞态的先后。

    ``all_expired`` 让超时扫描把每条待审批都当成已过期。
    """

    def __init__(self) -> None:
        super().__init__()
        self.hooks: dict[ApprovalStatus, _Hook] = {}
        #: 人工裁定赢下 CAS 之后、续跑写检查点之前跑一次(终审 C1 的竞态窗口)。
        self.after_human_win: _Hook | None = None
        self.all_expired = False

    async def mark_decided(self, **kwargs: Any) -> bool:
        hook = self.hooks.pop(kwargs["status"], None)
        if hook is not None:
            await hook()
        won = await super().mark_decided(**kwargs)
        after, human = self.after_human_win, kwargs["decided_by"] != VOIDED_BY
        if won and human and after is not None:
            self.after_human_win = None
            await after()
        return won

    async def list_expired(self, *, before: datetime, limit: int = 1000) -> list[ApprovalRecord]:
        if self.all_expired:
            before = datetime.now(UTC) + timedelta(days=365)
        return await super().list_expired(before=before, limit=limit)


def _settings() -> Settings:
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


@dataclass
class _Stack:
    app: Any
    client: AsyncClient
    tenant_id: UUID
    llm: _Llm
    tool: _Lookup
    approvals: _HookedApprovals
    run_store: InMemoryRunStore
    audit_store: InMemoryAuditLogStore
    graphs: list[Any]

    @property
    def runtime(self) -> AgentRuntime:
        return self.app.state.agent_runtime  # type: ignore[no-any-return]

    @property
    def graph(self) -> Any:
        assert len(self.graphs) == 1, "同一份配置应只构建一次(runtime 缓存)"
        return self.graphs[0]

    async def start(
        self, thread: UUID | None = None, *, mode: str = "stream"
    ) -> tuple[UUID, UUID, httpx.Response]:
        body: dict[str, Any] = {"user_id": _USER, "input": "查一下", "mode": mode}
        if thread is not None:
            body["session_id"] = str(thread)
        resp = await self.client.post(f"/v1/agents/{_AGENT}/runs", json=body)
        if mode == "queue":
            assert resp.status_code == 202, resp.text
            data = resp.json()["data"]
            return UUID(data["thread_id"]), UUID(data["run_id"]), resp
        assert resp.status_code == 200, resp.text
        _ = resp.text  # 读完整条 SSE —— 读到 end 帧时 worker 已收尾
        run_id = UUID(resp.headers["x-expert-work-run-id"])
        await self.settle(run_id)
        return UUID(resp.headers["x-expert-work-session-id"]), run_id, resp

    async def settle(self, run_id: UUID) -> None:
        record = self.runtime.run_manager.get(run_id)
        if record is not None and record.task is not None:
            await record.task

    async def decide(self, run_id: UUID, decision: str = "approve") -> httpx.Response:
        return await self.client.post(
            f"/v1/agents/{_AGENT}/runs/{run_id}:decide",
            json={"user_id": _USER, "decision": decision, "mode": "queue"},
        )

    async def approval(self, run_id: UUID) -> ApprovalRecord:
        record = await self.approvals.get_by_run(run_id=run_id, tenant_id=self.tenant_id)
        assert record is not None
        return record

    async def row(self, run_id: UUID) -> RunInfo:
        row = await self.run_store.get(run_id=run_id, tenant_id=self.tenant_id)
        assert row is not None
        return row

    async def run_ids(self, thread: UUID) -> list[UUID]:
        rows = await self.run_store.list_by_thread(thread_id=thread, tenant_id=self.tenant_id)
        return [r.run_id for r in rows]

    async def snapshot(self, thread: UUID) -> Any:
        return await self.graph.aget_state(
            {"configurable": {"thread_id": str(thread), "tenant_id": str(self.tenant_id)}}
        )

    async def void_audits(self) -> list[AuditEntry]:
        page = await self.audit_store.query(AuditQuery(tenant_id=self.tenant_id))
        return [
            e
            for e in page.entries
            if e.action.value == "approval:decided" and e.reason == VOID_AUDIT_REASON
        ]

    def queue_worker(self) -> RunQueueWorker:
        state = self.app.state
        return RunQueueWorker(
            run_store=self.run_store,
            thread_store=state.thread_meta_repo,
            agent_spec_store=state.agent_spec_repo,
            runtime=self.runtime,
            audit_logger=state.audit_logger,
            approval_store=self.approvals,
        )


def _runtime(
    *,
    run_store: InMemoryRunStore,
    event_store: InMemoryRunEventStore,
    llm: _Llm,
    tool: _Lookup,
    graphs: list[Any],
) -> AgentRuntime:
    saver = InMemorySaver()

    async def _build(
        spec: AgentSpec,
        *,
        tenant_id: UUID | None = None,
        user_id: str | None = None,
        token_usage_kind: str = "conversation",  # noqa: S107 — usage label, not a secret
    ) -> BuiltAgent:
        del spec, tenant_id, user_id, token_usage_kind
        registry = ToolRegistry()
        registry.register(tool)
        graph = GraphRunner(checkpointer=saver).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                approval_required_tools=frozenset({_GATED}),
            )
        )
        graphs.append(graph)
        return BuiltAgent(graph=graph, system_prompt="you are support", max_steps=6)

    return AgentRuntime(
        run_manager=RunManager(store=run_store),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_build,
        run_event_store=event_store,
    )


@pytest.fixture
def script() -> list[AIMessage]:
    return [_gated("A"), _gated("B")]


@pytest.fixture
async def s(script: list[AIMessage]) -> AsyncIterator[_Stack]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    event_store = InMemoryRunEventStore()
    audit_store = InMemoryAuditLogStore()
    approvals = _HookedApprovals()
    llm, tool = _Llm(script=list(script)), _Lookup()
    graphs: list[Any] = []
    app = create_app(
        settings=_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
        agent_runtime=_runtime(
            run_store=run_store, event_store=event_store, llm=llm, tool=tool, graphs=graphs
        ),
        run_repo=run_store,
        run_event_repo=event_store,
        approval_repo=approvals,
    )
    tenant_id = uuid4()
    await app.state.agent_spec_repo.create(
        tenant_id=tenant_id,
        spec=AgentSpec.model_validate(deepcopy(_SPEC)),
        spec_sha256="a" * 64,
        created_by="seed",
    )
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-integrator",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {jwt}"},
    ) as client:
        yield _Stack(
            app=app,
            client=client,
            tenant_id=tenant_id,
            llm=llm,
            tool=tool,
            approvals=approvals,
            run_store=run_store,
            audit_store=audit_store,
            graphs=graphs,
        )


async def _assert_voided(s: _Stack, run_a: UUID, *, by: UUID) -> None:
    approval = await s.approval(run_a)
    assert approval.status is ApprovalStatus.REJECTED
    assert approval.decided_by == VOIDED_BY
    assert approval.continuation_run_id is None
    row = await s.row(run_a)
    assert row.status is RunStatus.INTERRUPTED
    assert row.error == "new_turn"
    audits = [e for e in await s.void_audits() if e.resource_id == str(run_a)]
    assert len(audits) == 1
    # 只有 id,没有工具参数 / 输入值。
    assert set(audits[0].details) == {
        "thread_id",
        "decision",
        "status",
        "request_id",
        "voided_by_run_id",
    }
    assert audits[0].details["voided_by_run_id"] == str(by)


# ---------------------------------------------------------------------------
# A —— stream 新一轮:门控调用不执行、B 有自己的审批、A 被作废
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_turn_runs_behind_the_gate_and_voids_the_paused_turn(s: _Stack) -> None:
    thread, run_a, _ = await s.start()
    assert (await s.row(run_a)).status is RunStatus.PAUSED
    assert (await s.approval(run_a)).proposed_args == {"key": "A"}

    _, run_b, resp_b = await s.start(thread)

    assert s.tool.seen == [], "门控工具在没有审批的情况下被执行了"
    approval_b = await s.approval(run_b)
    assert approval_b.status is ApprovalStatus.PENDING
    assert approval_b.proposed_args == {"key": "B"}
    assert (await s.row(run_b)).status is RunStatus.PAUSED
    assert "event: approval" in resp_b.text
    await _assert_voided(s, run_a, by=run_b)
    end_user = await s.app.state.tenant_user_repo.resolve(
        tenant_id=s.tenant_id, subject_type="user", subject_id=f"ext:{_USER}"
    )
    (audit,) = await s.void_audits()
    assert audit.on_behalf_of == str(end_user.id)

    # B 的模型请求里每个工具调用都有结果 —— A 那条被作废的调用补上了「已作废」。
    assert sanitize_dangling_tool_calls(s.llm.prompts[-1]) == []
    voided = [m for m in s.llm.prompts[-1] if getattr(m, "tool_call_id", None) == "tc-A"]
    assert [m.content for m in voided] == [VOIDED_APPROVAL_CONTENT]

    # 已经结束的 A 不会再推送事件;事后回放 A 的事件流,收尾的 end 跟着 run 行走。
    if _BACKGROUND_PERSIST_WRITERS:
        await asyncio.gather(*_BACKGROUND_PERSIST_WRITERS, return_exceptions=True)
    replay = await s.client.get(f"/v1/agents/{_AGENT}/runs/{run_a}/events?user_id={_USER}")
    assert replay.status_code == 200, replay.text
    ends = [b for b in replay.text.split("\n\n") if "event: end" in b.splitlines()]
    assert len(ends) == 1
    assert json.loads(ends[0].rsplit("data: ", 1)[1])["status"] == "interrupted"


# ---------------------------------------------------------------------------
# B —— 作废之后再裁定 A:冲突、什么都不执行、检查点不动
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deciding_a_voided_approval_is_a_conflict_and_resumes_nothing(s: _Stack) -> None:
    thread, run_a, _ = await s.start()
    _, run_b, _ = await s.start(thread)
    before = await s.snapshot(thread)
    runs_before = await s.run_ids(thread)

    resp = await s.decide(run_a, "approve")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "APPROVAL_CONFLICT"
    assert s.tool.seen == []
    assert (await s.snapshot(thread)).config == before.config
    assert await s.run_ids(thread) == runs_before
    assert (await s.approval(run_b)).status is ApprovalStatus.PENDING

    # B 自己的审批照常可批,批了才执行,执行的是 B 的参数。
    ok = await s.decide(run_b, "approve")
    assert ok.status_code == 202, ok.text
    await s.settle(UUID(ok.json()["data"]["run_id"]))
    assert s.tool.seen == [{"key": "B"}]


# ---------------------------------------------------------------------------
# C —— queue 模式:入队即作废;出队执行时门控照样生效
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_new_turn_voids_at_enqueue_and_runs_behind_the_gate(s: _Stack) -> None:
    thread, run_a, _ = await s.start()

    _, run_b, _ = await s.start(thread, mode="queue")
    await _assert_voided(s, run_a, by=run_b)

    assert await s.queue_worker().run_once() == 1
    await s.settle(run_b)

    assert s.tool.seen == []
    assert (await s.approval(run_b)).proposed_args == {"key": "B"}
    assert (await s.row(run_b)).status is RunStatus.PAUSED


@pytest.mark.parametrize("script", [[AIMessage(content="hi"), _gated("A"), _gated("B")]])
@pytest.mark.asyncio
async def test_queued_turn_voids_an_approval_that_appeared_after_it_was_enqueued(
    s: _Stack,
) -> None:
    """入队时会话还没停在审批上;出队前 A 停下了 —— 出队这一侧必须补上作废。"""
    thread, _, _ = await s.start()
    _, run_b, _ = await s.start(thread, mode="queue")
    _, run_a, _ = await s.start(thread)
    assert (await s.approval(run_a)).status is ApprovalStatus.PENDING

    assert await s.queue_worker().run_once() == 1
    await s.settle(run_b)

    await _assert_voided(s, run_a, by=run_b)
    assert s.tool.seen == []
    assert (await s.approval(run_b)).proposed_args == {"key": "B"}
    resp = await s.decide(run_a, "approve")
    assert resp.status_code == 409, resp.text
    assert s.tool.seen == []


# ---------------------------------------------------------------------------
# 裁定咽喉 —— 审批所在的 run 之后已经有更新的 run(登记窗口 / 遗留数据)
# ---------------------------------------------------------------------------


async def _newer_row(s: _Stack, thread: UUID) -> UUID:
    """直接落一行更新的 run —— 模拟新一轮建行早于这条审批登记(或修复前的遗留数据)。"""
    newer = uuid4()
    now = datetime.now(UTC) + timedelta(seconds=1)
    await s.run_store.create(
        RunInfo(
            run_id=newer,
            tenant_id=s.tenant_id,
            thread_id=thread,
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CONTINUE,
            is_resume=True,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=now,
        )
    )
    return newer


@pytest.mark.asyncio
async def test_deciding_an_approval_older_than_the_threads_latest_run_voids_it(
    s: _Stack,
) -> None:
    thread, run_a, _ = await s.start()
    newer = await _newer_row(s, thread)
    before = await s.snapshot(thread)

    resp = await s.decide(run_a, "approve")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "APPROVAL_CONFLICT"
    assert s.tool.seen == []
    await _assert_voided(s, run_a, by=newer)
    # 这条路不碰检查点:它已经属于更新的那一轮。
    assert (await s.snapshot(thread)).config == before.config
    assert await s.run_ids(thread) == [run_a, newer]


@pytest.mark.asyncio
async def test_timeout_sweep_voids_instead_of_resuming_a_superseded_approval(s: _Stack) -> None:
    thread, run_a, _ = await s.start()
    newer = await _newer_row(s, thread)
    s.approvals.all_expired = True
    state = s.app.state
    sweep = ApprovalTimeoutSweep(
        approval_store=s.approvals,
        thread_store=state.thread_meta_repo,
        agent_spec_store=state.agent_spec_repo,
        runtime=s.runtime,
        audit_logger=state.audit_logger,
    )

    assert await sweep.run_once() == 0

    await _assert_voided(s, run_a, by=newer)
    assert await s.run_ids(thread) == [run_a, newer]
    # 已不再是 pending —— 下一轮扫描不会反复捡到它。
    assert await sweep.run_once() == 0


# ---------------------------------------------------------------------------
# F —— 竞态:作废与裁定只有一个赢
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [[_gated("A"), AIMessage(content="a-done"), _gated("B")]])
@pytest.mark.asyncio
async def test_a_decision_that_completed_first_runs_once_and_the_new_turn_is_gated(
    s: _Stack,
) -> None:
    """裁定先完成(续跑已跑完):批准的调用执行一次;之后的新一轮各过各的审批门。"""
    thread, run_a, _ = await s.start()
    ok = await s.decide(run_a, "approve")
    assert ok.status_code == 202, ok.text
    await s.settle(UUID(ok.json()["data"]["run_id"]))
    assert s.tool.seen == [{"key": "A"}]

    _, run_b, _ = await s.start(thread)

    assert s.tool.seen == [{"key": "A"}]
    assert (await s.approval(run_a)).status is ApprovalStatus.APPROVED
    assert await s.void_audits() == []
    approval_b = await s.approval(run_b)
    assert approval_b.status is ApprovalStatus.PENDING
    assert approval_b.proposed_args == {"key": "B"}
    assert sanitize_dangling_tool_calls(s.llm.prompts[-1]) == []


@pytest.mark.asyncio
async def test_a_decision_that_only_won_the_cas_does_not_let_the_new_turn_void_it(
    s: _Stack,
) -> None:
    """人工裁定先赢下审批行的 CAS:新一轮的作废输掉,不写审计、不动这条裁定;
    新一轮照常跑,审批门照样生效,历史照样收口。"""
    thread, run_a, _ = await s.start()

    async def human_decides_first() -> None:
        assert await s.approvals.mark_decided(
            run_id=run_a,
            tenant_id=s.tenant_id,
            status=ApprovalStatus.APPROVED,
            decided_by="human",
            decided_at=datetime.now(UTC),
            continuation_run_id=uuid4(),
        )

    s.approvals.hooks[ApprovalStatus.REJECTED] = human_decides_first

    _, run_b, _ = await s.start(thread)

    approval_a = await s.approval(run_a)
    assert approval_a.status is ApprovalStatus.APPROVED
    assert approval_a.decided_by == "human"
    assert await s.void_audits() == []
    assert s.tool.seen == []
    assert (await s.approval(run_b)).proposed_args == {"key": "B"}
    assert sanitize_dangling_tool_calls(s.llm.prompts[-1]) == []


def _modify_to_x() -> dict[str, Any]:
    return {"decision": "modify", "modified_args": {"key": "X"}}


def _approve() -> dict[str, Any]:
    return {"decision": "approve"}


@pytest.mark.parametrize(
    ("script", "decision"),
    [
        ([_gated("A"), _gated("B")], _modify_to_x()),
        # 新一轮的调用与 A 参数相同(只是 call id 不同),摘要核对拦不住。
        (
            [
                _gated("A"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": _GATED, "args": {"key": "A"}, "id": "tc-A2", "type": "tool_call"}
                    ],
                ),
            ],
            _approve(),
        ),
    ],
    ids=["modify", "approve-same-args"],
)
@pytest.mark.asyncio
async def test_a_new_turn_inside_the_decision_window_is_never_resumed_by_that_decision(
    s: _Stack, decision: dict[str, Any]
) -> None:
    """终审 C1:裁定赢下 CAS 之后、写检查点之前,新一轮跑到了自己的审批门。
    这次裁定不能写进已经属于新一轮的检查点、更不能执行新一轮的调用。"""
    thread, run_a, _ = await s.start()
    box: dict[str, UUID] = {}

    async def new_turn_runs_in_the_window() -> None:
        _, box["run_b"], _ = await s.start(thread)

    s.approvals.after_human_win = new_turn_runs_in_the_window
    body = {"user_id": _USER, "mode": "queue", "idempotency_key": "decide-1", **decision}

    resp = await s.client.post(f"/v1/agents/{_AGENT}/runs/{run_a}:decide", json=body)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "APPROVAL_CONFLICT"
    run_b = box["run_b"]
    assert s.tool.seen == [], "裁定被套到了新一轮的调用上"
    approval_b = await s.approval(run_b)
    assert approval_b.status is ApprovalStatus.PENDING
    assert (await s.row(run_b)).status is RunStatus.PAUSED
    assert sanitize_dangling_tool_calls(s.llm.prompts[-1]) == []
    approval_a = await s.approval(run_a)
    assert approval_a.status is ApprovalStatus.REJECTED
    assert approval_a.decided_by == VOIDED_BY
    assert approval_a.continuation_run_id is None
    row_a = await s.row(run_a)
    assert (row_a.status, row_a.error) == (RunStatus.INTERRUPTED, "new_turn")
    assert await s.run_ids(thread) == [run_a, run_b], "不该建续跑行"
    audits = [e for e in await s.void_audits() if e.resource_id == str(run_a)]
    assert [a.details["voided_by_run_id"] for a in audits] == [str(run_b)]
    before = await s.snapshot(thread)

    # 同一个请求重放:同一个 409,不会拿到一个从未创建过的 run id。
    replay = await s.client.post(f"/v1/agents/{_AGENT}/runs/{run_a}:decide", json=body)

    assert replay.status_code == 409, replay.text
    assert (await s.snapshot(thread)).config == before.config
    assert s.tool.seen == []


@pytest.mark.asyncio
async def test_a_build_failure_leaves_the_approval_pending(
    s: _Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """构建在 CAS 之前:构建失败时这条审批仍可裁定,修好之后照常续跑。"""
    _, run_a, _ = await s.start()
    real_get_agent = s.runtime.get_agent

    async def broken(**_: Any) -> BuiltAgent:
        raise AgentFactoryError("boom")

    monkeypatch.setattr(s.runtime, "get_agent", broken)
    failed = await s.decide(run_a, "approve")
    assert failed.status_code == 422, failed.text
    assert (await s.approval(run_a)).status is ApprovalStatus.PENDING

    monkeypatch.setattr(s.runtime, "get_agent", real_get_agent)
    ok = await s.decide(run_a, "approve")
    assert ok.status_code == 202, ok.text
    await s.settle(UUID(ok.json()["data"]["run_id"]))
    assert s.tool.seen == [{"key": "A"}]


@pytest.mark.asyncio
async def test_a_deleted_agent_still_consumes_the_decision(s: _Stack) -> None:
    """agent 已删除是永久失败:沿用原语义先消费裁定再 410,超时扫描不会反复捡回它。"""
    _, run_a, _ = await s.start()
    await s.app.state.agent_spec_repo.update_status(
        tenant_id=s.tenant_id, name=_AGENT, version="1.0.0", status=AgentSpecStatus.DELETED
    )

    resp = await s.decide(run_a, "approve")

    assert resp.status_code == 410, resp.text
    assert (await s.approval(run_a)).status is ApprovalStatus.APPROVED
    assert s.tool.seen == []


@pytest.mark.asyncio
async def test_a_new_turn_that_voids_first_makes_the_decision_a_conflict(s: _Stack) -> None:
    thread, run_a, _ = await s.start()
    new_run = uuid4()

    async def new_turn_voids_first() -> None:
        voided = await void_pending_approvals(
            thread_id=thread,
            tenant_id=s.tenant_id,
            new_run_id=new_run,
            approvals=s.approvals,
            run_manager=s.runtime.run_manager,
            audit=s.app.state.audit_logger,
            actor_id="sa-integrator",
        )
        assert voided == (run_a,)

    s.approvals.hooks[ApprovalStatus.APPROVED] = new_turn_voids_first

    resp = await s.decide(run_a, "approve")

    assert resp.status_code == 409, resp.text
    assert s.tool.seen == []
    assert await s.run_ids(thread) == [run_a]
    await _assert_voided(s, run_a, by=new_run)
    values = (await s.snapshot(thread)).values
    assert values.get("approval_resume") is None


# ---------------------------------------------------------------------------
# 终审 I1 —— 按历史的形状收口:作废主路径之外留下的没有结果的工具调用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script", [[_gated("A"), AIMessage(content="b-done"), AIMessage(content="c-done")]]
)
@pytest.mark.asyncio
async def test_a_new_turn_inside_the_registration_window_leaves_no_unanswered_call(
    s: _Stack,
) -> None:
    """A 已经 PAUSED、审批行还没落库时 B 开跑:B 与之后的 C 的历史都完整。"""
    real_create = s.approvals.create
    box: dict[str, UUID] = {}

    async def create(record: ApprovalRecord) -> ApprovalRecord:
        if "run_b" not in box:
            _, box["run_b"], _ = await s.start(record.thread_id)
        return await real_create(record)

    s.approvals.create = create  # type: ignore[method-assign]
    thread, run_a, _ = await s.start()
    run_b = box["run_b"]

    assert (await s.row(run_b)).status is RunStatus.SUCCESS
    assert sanitize_dangling_tool_calls(s.llm.prompts[1]) == []
    assert (await s.approval(run_a)).status is ApprovalStatus.PENDING
    row_a = await s.row(run_a)
    assert (row_a.status, row_a.error) == (RunStatus.INTERRUPTED, "new_turn")

    _, run_c, _ = await s.start(thread)

    assert sanitize_dangling_tool_calls(s.llm.prompts[2]) == []
    await _assert_voided(s, run_a, by=run_c)
    assert s.tool.seen == []
    resp = await s.decide(run_a, "approve")
    assert resp.status_code == 409, resp.text
    assert s.tool.seen == []


@pytest.mark.parametrize("script", [[_gated("A"), AIMessage(content="b-done")]])
@pytest.mark.asyncio
async def test_a_void_that_failed_halfway_is_finished_by_the_next_turn(
    s: _Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """作废赢下 CAS 之后收 run 行失败:这次请求报错;下一轮补收 run 行、收口历史。"""
    thread, run_a, _ = await s.start()
    manager = s.runtime.run_manager
    real_close = manager.close_paused
    calls: list[UUID] = []

    async def flaky_close(run_id: UUID, **kwargs: Any) -> bool:
        calls.append(run_id)
        if len(calls) == 1:
            raise RuntimeError("store down")
        return await real_close(run_id, **kwargs)

    monkeypatch.setattr(manager, "close_paused", flaky_close)
    with pytest.raises(RuntimeError, match="store down"):
        await s.start(thread)
    assert (await s.approval(run_a)).status is ApprovalStatus.REJECTED
    assert (await s.row(run_a)).status is RunStatus.PAUSED
    assert await s.run_ids(thread) == [run_a]

    _, run_b, _ = await s.start(thread)

    assert sanitize_dangling_tool_calls(s.llm.prompts[-1]) == []
    row_a = await s.row(run_a)
    assert (row_a.status, row_a.error) == (RunStatus.INTERRUPTED, "new_turn")
    assert len([e for e in await s.void_audits() if e.resource_id == str(run_a)]) == 1
    assert (await s.row(run_b)).status is RunStatus.SUCCESS
    assert s.tool.seen == []


@pytest.mark.parametrize("script", [[AIMessage(content="a-done"), AIMessage(content="b-done")]])
@pytest.mark.asyncio
async def test_a_turn_cancelled_before_its_tools_ran_is_closed_by_the_next_turn(
    s: _Stack,
) -> None:
    """取消落在「模型给出调用」与「工具执行」之间:没有审批,尾巴照样悬空,下一轮收口。"""
    thread, _, _ = await s.start()
    cancelled = uuid4()
    now = datetime.now(UTC) + timedelta(seconds=1)
    await s.run_store.create(
        RunInfo(
            run_id=cancelled,
            tenant_id=s.tenant_id,
            thread_id=thread,
            user_id=None,
            status=RunStatus.INTERRUPTED,
            on_disconnect=DisconnectMode.CONTINUE,
            is_resume=True,
            error="user_cancel",
            created_at=now,
            updated_at=now,
            finished_at=now,
        )
    )
    dangling = AIMessage(
        content="",
        tool_calls=[{"name": _GATED, "args": {"key": "Z"}, "id": "tc-Z", "type": "tool_call"}],
        additional_kwargs={STAMP_RUN_ID: str(cancelled)},
    )
    await s.graph.aupdate_state(
        {"configurable": {"thread_id": str(thread), "tenant_id": str(s.tenant_id)}},
        {"messages": [dangling]},
        as_node="agent",
    )

    _, run_b, _ = await s.start(thread)

    prompt = s.llm.prompts[-1]
    assert sanitize_dangling_tool_calls(prompt) == []
    results = [m.content for m in prompt if getattr(m, "tool_call_id", None) == "tc-Z"]
    assert results == [UNRUN_TOOL_CALL_CONTENT]
    assert (await s.row(run_b)).status is RunStatus.SUCCESS
    assert s.tool.seen == []
    assert await s.void_audits() == []


@pytest.mark.asyncio
async def test_an_approval_registered_after_the_void_is_voided_by_the_repair(s: _Stack) -> None:
    """待审批在新一轮的作废查询之后才登记:收口尾巴时照常作废它。"""
    thread, run_a, _ = await s.start()
    new_run = uuid4()

    closed = await repair_turn_tail(
        graph=s.graph,
        thread_id=thread,
        tenant_id=s.tenant_id,
        new_run_id=new_run,
        approvals=s.approvals,
        run_manager=s.runtime.run_manager,
        audit=s.app.state.audit_logger,
        actor_id="sa-integrator",
    )

    assert closed == 1
    await _assert_voided(s, run_a, by=new_run)


@pytest.mark.asyncio
async def test_a_tail_whose_run_is_still_running_is_left_alone() -> None:
    """尾巴所属的 run 还在跑(并发的一轮):不补、不写检查点。"""
    run_store = InMemoryRunStore()
    manager = RunManager(store=run_store)
    thread, tenant, live = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    await run_store.create(
        RunInfo(
            run_id=live,
            tenant_id=tenant,
            thread_id=thread,
            user_id=None,
            status=RunStatus.RUNNING,
            on_disconnect=DisconnectMode.CONTINUE,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
        )
    )
    tail = AIMessage(
        content="",
        tool_calls=[{"name": _GATED, "args": {}, "id": "tc-live", "type": "tool_call"}],
        additional_kwargs={STAMP_RUN_ID: str(live)},
    )

    class _LiveTail:
        async def aget_state(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(values={"messages": [tail]})

        async def aupdate_state(self, *_: Any, **__: Any) -> None:
            raise AssertionError("还在跑的一轮不能被收口")

    closed = await repair_turn_tail(
        graph=_LiveTail(),
        thread_id=thread,
        tenant_id=tenant,
        new_run_id=uuid4(),
        approvals=InMemoryApprovalStore(),
        run_manager=manager,
        audit=build_default_audit_logger(InMemoryAuditLogStore()),
        actor_id="t",
    )
    assert closed == 0


@pytest.mark.asyncio
async def test_after_a_successful_turn_the_checkpoint_is_not_read() -> None:
    """最近一轮成功结束:不去读检查点(绝大多数新一轮的开销只有一条查询)。"""
    run_store = InMemoryRunStore()
    manager = RunManager(store=run_store)
    thread, tenant, done = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    await run_store.create(
        RunInfo(
            run_id=done,
            tenant_id=tenant,
            thread_id=thread,
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CONTINUE,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=now,
        )
    )

    class _Unreadable:
        async def aget_state(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("不该读检查点")

    closed = await repair_turn_tail(
        graph=_Unreadable(),
        thread_id=thread,
        tenant_id=tenant,
        new_run_id=uuid4(),
        approvals=InMemoryApprovalStore(),
        run_manager=manager,
        audit=build_default_audit_logger(InMemoryAuditLogStore()),
        actor_id="t",
    )
    assert closed == 0


# ---------------------------------------------------------------------------
# 入口清零 —— 每一轮的图输入都把审批三件套清零
# ---------------------------------------------------------------------------


def test_every_turn_input_resets_the_approval_channels() -> None:
    built = BuiltAgent(graph=None, system_prompt="sys", max_steps=3)  # type: ignore[arg-type]
    fresh = build_run_graph_input(
        built, input_text="hi", image_refs=[], untrusted_content=None, run_id=uuid4()
    )
    replay = replay_graph_input(
        built, [SystemMessage(content="sys"), HumanMessage(content="hi")], run_id=uuid4()
    )
    for graph_input in (fresh, replay):
        for key, value in APPROVAL_TURN_RESET.items():
            assert key in graph_input
            assert graph_input[key] is value
