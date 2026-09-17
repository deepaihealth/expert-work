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

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from control_plane.approval_void import VOID_AUDIT_REASON, VOIDED_BY, void_pending_approvals
from control_plane.audit import build_default_audit_logger
from control_plane.run_queue_worker import RunQueueWorker
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence import InMemoryApprovalStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import (
    AgentSpec,
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
    BuiltAgent,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
    sanitize_dangling_tool_calls,
)
from orchestrator.approval_turn import VOIDED_APPROVAL_CONTENT
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
        self.all_expired = False

    async def mark_decided(self, **kwargs: Any) -> bool:
        hook = self.hooks.pop(kwargs["status"], None)
        if hook is not None:
            await hook()
        return await super().mark_decided(**kwargs)

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

    # B 的模型请求里每个工具调用都有结果 —— A 那条被作废的调用补上了「已作废」。
    assert sanitize_dangling_tool_calls(s.llm.prompts[-1]) == []
    voided = [m for m in s.llm.prompts[-1] if getattr(m, "tool_call_id", None) == "tc-A"]
    assert [m.content for m in voided] == [VOIDED_APPROVAL_CONTENT]


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


@pytest.mark.asyncio
async def test_a_decision_that_lands_first_wins_and_the_new_turn_does_not_void(
    s: _Stack,
) -> None:
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
    assert (await s.row(run_a)).status is RunStatus.PAUSED
    assert await s.void_audits() == []
    # 作废没赢,就不收口检查点;新一轮照常跑,审批门照样生效。
    messages = (await s.snapshot(thread)).values["messages"]
    assert all(getattr(m, "content", None) != VOIDED_APPROVAL_CONTENT for m in messages)
    assert s.tool.seen == []
    assert (await s.approval(run_b)).proposed_args == {"key": "B"}


@pytest.mark.asyncio
async def test_a_new_turn_that_voids_first_makes_the_decision_a_conflict(s: _Stack) -> None:
    thread, run_a, _ = await s.start()
    new_run = uuid4()

    async def new_turn_voids_first() -> None:
        voided = await void_pending_approvals(
            graph=s.graph,
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
