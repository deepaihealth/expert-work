"""B-61 续跑缺口 —— 每个 run 入口都要带着「这一轮」的 Dynamic-Prompt 原始 inputs 跑。

B-61 让 inputs 除了渲染提示词还做两件事:``inputs`` 节点写 ``inputs/<run_id>/inputs.json``、
``tools_node`` 用它填平台绑定的 MCP 参数。两件事都读 ``configurable[PROMPT_INPUTS_KEY]``,
而这个值此前只有「新开一轮」的入口给。本文件逐个入口钉住:

* 审批续跑(新 run_id、``graph_input=None``),含「续跑又暂停、再续跑」;
* 孤儿复活(同 run_id,**另一个副本**接手);
* ``:regenerate``(stream / queue)与 ``:edit``;
* 非 jinja agent 什么都不变。

**夹具走整条真路径**:真 HTTP 端点 → 真 ``run_agent`` → 真 react 图(真绑定、真审批门、
真 ``inputs`` 节点、真 ``exec_python``)→ 真内存 store。续跑的 config 由生产代码
(``resolve_approval_decision`` / ``OrphanSweep`` / ``RunQueueWorker``)自己建,不复用首段的
config 对象 —— 复用正是 ``test_approving_a_paused_call_dispatches_the_bound_value`` 看不见
这个缺陷的原因。替身只有三个:LLM(脚本)、MCP 服务器、沙箱。
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.orphan_sweep import OrphanSweep
from control_plane.run_queue_worker import RunQueueWorker
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import (
    AgentSpec,
    ArgBindingSpec,
    AuditAction,
    AuditEntry,
    MCPToolSpec,
    PromptVariableSpec,
)
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunManager,
    RunStatus,
)
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import BuiltAgent, GraphRunner, ToolSpec, build_react_graph
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.graph_builder.inputs_node import make_inputs_node
from orchestrator.sse import SYSTEM_PROMPT_EVENT
from orchestrator.tools import (
    ExecPythonTool,
    MCPServerPool,
    MCPToolDef,
    RecordingMCPClient,
    RecordingSandboxRuntime,
    SandboxOutcome,
    ToolContext,
    ToolEnv,
    ToolResult,
    build_tool_registry,
)
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.inputs_doc import inputs_rel_path
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_AGENT = "carry-bot"
_USER = "cust-77"
_SERVER = "deepcare"
_WIRE = f"mcp__{_SERVER}__t1"
_VALUE = "PRJ-7f3a-0192"
_VARIABLE = "pc"
_INPUTS = {_VARIABLE: _VALUE}

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

_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"project_code": {"type": "string"}, "keyword": {"type": "string"}},
    "required": ["project_code", "keyword"],
}


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


def _calls(*calls: tuple[str, dict[str, Any]]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": dict(args), "id": f"tc-{uuid4().hex[:8]}", "type": "tool_call"}
            for name, args in calls
        ],
    )


_BOUND_CALL = (_WIRE, {"keyword": "王"})
_EXEC_CALL = ("exec_python", {"code": "print(1)"})
_PROBE_CALL: tuple[str, dict[str, Any]] = ("probe", {})
_DONE = AIMessage(content="done")
#: 脚本里的这一步永远不返回 —— 模拟「副本在这一刻死了」。
_HANG = "hang"


class _ScriptedLLM:
    """按脚本逐次返回;脚本走完一直回 ``_DONE``。"""

    def __init__(self, script: Sequence[AIMessage | str]) -> None:
        self._script = list(script)
        self.calls = 0
        self.hung = asyncio.Event()

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **kwargs: Any
    ) -> AIMessage:
        del messages, tools, kwargs
        index = self.calls
        self.calls += 1
        step = self._script[index] if index < len(self._script) else _DONE
        if isinstance(step, str):
            self.hung.set()
            await asyncio.Event().wait()
        assert isinstance(step, AIMessage)
        return step


@dataclass
class _ContextProbe:
    """记下派发时拿到的 ``ToolContext`` —— 委派子代的 config 就从它派生。"""

    seen: list[ToolContext] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="probe",
            description="records its tool context",
            parameters={"type": "object", "properties": {}},
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args
        self.seen.append(ctx)
        return ToolResult(content="ok")


class _RecordingAuditStore(InMemoryAuditLogStore):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> AuditEntry:
        stamped = await super().append(entry)
        self.rows.append(stamped)
        return stamped


def _written_docs(writer: RecordingSandboxRuntime) -> dict[str, dict[str, Any]]:
    """``inputs`` 节点经沙箱写下的文件:相对路径 → 解析后的 JSON。

    写文件的代码片段首行是 ``_PARAMS = '<json>'``(``file_ops._snippet``),照原样解出来,
    不去猜片段的其余部分。
    """
    docs: dict[str, dict[str, Any]] = {}
    for _sandbox_id, code in writer.execs:
        first = code.split("\n", 1)[0]
        if not first.startswith("_PARAMS = "):
            continue
        params = json.loads(ast.literal_eval(first.removeprefix("_PARAMS = ")))
        docs[params["rel"]] = json.loads(params["content"])
    return docs


# ---------------------------------------------------------------------------
# 整栈夹具
# ---------------------------------------------------------------------------


@dataclass
class _Stack:
    client: AsyncClient
    app: Any
    headers: dict[str, str]
    tenant_id: UUID
    runs: InMemoryRunStore
    events: InMemoryRunEventStore
    built: BuiltAgent
    mcp: RecordingMCPClient
    sandbox: RecordingSandboxRuntime
    writer: RecordingSandboxRuntime
    probe: _ContextProbe
    llm: _ScriptedLLM
    audit: _RecordingAuditStore

    async def start(
        self, *, mode: str = "stream", inputs: Mapping[str, Any] | None = None
    ) -> tuple[UUID, UUID]:
        body: dict[str, Any] = {"user_id": _USER, "input": "查一下", "mode": mode}
        if inputs is not None:
            body["inputs"] = dict(inputs)
        resp = await self.client.post(f"/v1/agents/{_AGENT}/runs", json=body, headers=self.headers)
        assert resp.status_code in (200, 202), resp.text
        if mode == "stream":
            return UUID(resp.headers["X-Expert-Work-Session-Id"]), UUID(
                resp.headers["X-Expert-Work-Run-Id"]
            )
        data = resp.json()["data"]
        return UUID(data["thread_id"]), UUID(data["run_id"])

    async def decide(self, run_id: UUID) -> UUID:
        resp = await self.client.post(
            f"/v1/agents/{_AGENT}/runs/{run_id}:decide",
            json={"user_id": _USER, "decision": "approve", "mode": "queue"},
            headers=self.headers,
        )
        assert resp.status_code == 202, resp.text
        return UUID(resp.json()["data"]["run_id"])

    async def post_supersede(self, op: str, run_id: UUID, body: dict[str, Any]) -> Any:
        return await self.client.post(
            f"/v1/agents/{_AGENT}/runs/{run_id}:{op}",
            json={"user_id": _USER, **body},
            headers=self.headers,
        )

    async def wait(self, run_id: UUID, *, timeout: float = 5.0) -> RunStatus:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            row = await self.runs.get(run_id=run_id, tenant_id=self.tenant_id)
            if row is not None and row.status in TERMINAL_RUN_STATUSES:
                return row.status
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"run {run_id} did not finish: {row}")
            await asyncio.sleep(0.01)

    async def prompt_frame(self, run_id: UUID) -> dict[str, Any] | None:
        """这个 run 落库的 ``system_prompt`` 帧;持久化是后台批写的,等它落下来。"""
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            frames = await self.events.list(run_id=run_id, event_names={SYSTEM_PROMPT_EVENT})
            if frames:
                data: dict[str, Any] = frames[0].data
                return data
            if asyncio.get_running_loop().time() > deadline:
                return None
            await asyncio.sleep(0.01)

    def worker(self, runtime: AgentRuntime | None = None) -> RunQueueWorker:
        return RunQueueWorker(
            run_store=self.runs,
            thread_store=self.app.state.thread_meta_repo,
            agent_spec_store=self.app.state.agent_spec_repo,
            runtime=runtime or self.app.state.agent_runtime,
            audit_logger=self.app.state.audit_logger,
            approval_store=self.app.state.approval_store,
        )

    def bound_values(self) -> list[Any]:
        """MCP 服务器每次真收到的被绑参数(没收到就是 ``None``)。"""
        return [args.get("project_code") for _name, args in self.mcp.calls]

    def drift_rows(self) -> list[AuditEntry]:
        return [r for r in self.audit.rows if r.action is AuditAction.APPROVAL_BINDING_DRIFT]


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


async def _build_agent(
    llm: _ScriptedLLM,
    *,
    jinja: bool,
    gated: frozenset[str],
    mcp: RecordingMCPClient,
    sandbox: RecordingSandboxRuntime,
    writer: RecordingSandboxRuntime,
    probe: _ContextProbe,
) -> BuiltAgent:
    pool = MCPServerPool()
    await pool.add(_SERVER, mcp)
    bindings = (
        [ArgBindingSpec(server=_SERVER, tool="t1", args={"project_code": _VARIABLE})]
        if jinja
        else []
    )
    registry = await build_tool_registry(
        [MCPToolSpec(servers=[_SERVER], arg_bindings=bindings)],
        tool_env=ToolEnv(mcp_pool=pool),
    )
    registry.register(ExecPythonTool(client=sandbox))
    registry.register(probe)
    variables = (PromptVariableSpec(name=_VARIABLE),) if jinja else ()
    graph = GraphRunner(checkpointer=InMemorySaver()).compile(
        build_react_graph(
            llm_caller=llm,
            tool_registry=registry,
            approval_required_tools=gated,
            inputs_node=make_inputs_node(client=writer, variables=variables) if jinja else None,
        )
    )
    return BuiltAgent(
        graph=graph,
        system_prompt="项目 {{ pc }}" if jinja else "你是助手",
        max_steps=10,
        prompt_jinja=jinja,
        prompt_variables=variables,
        prompt_base="项目 {{ pc }}" if jinja else "",
    )


def _runtime(
    built: BuiltAgent,
    *,
    runs: InMemoryRunStore,
    events: InMemoryRunEventStore,
    instance_id: str,
) -> AgentRuntime:
    """一个「副本」。检查点与两张表是共享的(生产里是 Postgres),进程内的东西各有一份。"""

    async def _builder(spec: object, **kwargs: Any) -> BuiltAgent:
        del spec, kwargs
        return built

    return AgentRuntime(
        run_manager=RunManager(store=runs, instance_id=instance_id),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,
        run_event_store=events,
    )


@asynccontextmanager
async def _stack(
    script: Sequence[AIMessage | str],
    *,
    jinja: bool = True,
    gated: frozenset[str] = frozenset(),
) -> AsyncIterator[_Stack]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    runs = InMemoryRunStore()
    events = InMemoryRunEventStore()
    llm = _ScriptedLLM(script)
    mcp = RecordingMCPClient(
        tools=(MCPToolDef(name="t1", description="t1", input_schema=_TOOL_SCHEMA),),
        responses={"t1": "result of t1"},
    )
    sandbox = RecordingSandboxRuntime()
    # inputs 节点经 ``SandboxWorkspaceWriter`` 写文件,它要解析 stdout 上的 JSON 信封。
    writer = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout='{"ok": true}', stderr="", exit_code=0, timed_out=False)
    )
    probe = _ContextProbe()
    built = await _build_agent(
        llm, jinja=jinja, gated=gated, mcp=mcp, sandbox=sandbox, writer=writer, probe=probe
    )
    audit = _RecordingAuditStore()
    app = create_app(
        settings=_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit),
        agent_runtime=_runtime(built, runs=runs, events=events, instance_id="pod-a"),
        run_repo=runs,
        run_event_repo=events,
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
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test") as client:
        stack = _Stack(
            client=client,
            app=app,
            headers={"Authorization": f"Bearer {jwt}"},
            tenant_id=tenant_id,
            runs=runs,
            events=events,
            built=built,
            mcp=mcp,
            sandbox=sandbox,
            writer=writer,
            probe=probe,
            llm=llm,
            audit=audit,
        )
        try:
            yield stack
        finally:
            await _cancel_leftover_tasks(stack.app.state.agent_runtime)


async def _cancel_leftover_tasks(runtime: AgentRuntime) -> None:
    """孤儿用例里「死掉的副本」那个任务永远挂着 —— 用例结束时收掉,别留给事件循环。"""
    for record in list(runtime.run_manager._runs.values()):
        task = record.task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


# ---------------------------------------------------------------------------
# A —— 审批续跑:被绑参数照样填上,RT-6 摘要对得上
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approving_a_bound_call_dispatches_the_bound_value() -> None:
    async with _stack([_calls(_BOUND_CALL), _DONE], gated=frozenset({_WIRE})) as s:
        _thread, r0 = await s.start(inputs=_INPUTS)
        assert await s.wait(r0) is RunStatus.PAUSED
        assert s.mcp.calls == [], "审批门真的拦住了 —— 否则后面的断言说明不了续跑"

        c1 = await s.decide(r0)
        assert await s.wait(c1) is RunStatus.SUCCESS

        assert s.bound_values() == [_VALUE]
        assert s.drift_rows() == [], "续跑段按空 inputs 重填 → 摘要对不上 → 完整性否决"


@pytest.mark.asyncio
async def test_a_resumed_run_that_pauses_again_still_carries_the_turn_inputs() -> None:
    """A + B —— 暂停 → 批准 → 又暂停 → 批准。第二段续跑的上一段本身就是续跑,
    它的 run 行没有 ``system_prompt`` 帧;两段都得找回这一轮第一个 run。"""
    script = [
        _calls(_BOUND_CALL),
        _calls(_BOUND_CALL),
        _calls(_EXEC_CALL, _PROBE_CALL),
        _DONE,
    ]
    async with _stack(script, gated=frozenset({_WIRE})) as s:
        _thread, r0 = await s.start(inputs=_INPUTS)
        assert await s.wait(r0) is RunStatus.PAUSED
        c1 = await s.decide(r0)
        assert await s.wait(c1) is RunStatus.PAUSED
        c2 = await s.decide(c1)
        assert await s.wait(c2) is RunStatus.SUCCESS

        assert s.bound_values() == [_VALUE, _VALUE]
        assert s.drift_rows() == []

        # B —— inputs.json 只在这一轮开头写过一次,写在 r0 名下;续跑段的沙箱指向它。
        docs = _written_docs(s.writer)
        assert list(docs) == [inputs_rel_path(r0)]
        assert docs[inputs_rel_path(r0)]["variables"][_VARIABLE]["value"] == _VALUE
        assert s.sandbox.exec_run_ids == [r0]

        # 续跑段委派出去的子代:同一个目录、同一份值(``_child_config`` 从父的
        # ToolContext 派生,这里拿的就是续跑段真实派发时的那个)。
        (ctx,) = s.probe.seen
        assert (ctx.run_id, ctx.inputs_run_id) == (c2, r0)
        child = _build_tool_context(_child_config(ctx, sub_thread_id=uuid4(), sub_run_id=uuid4()))
        assert child.inputs_run_id == r0
        assert dict(child.prompt_inputs or {}) == _INPUTS


# ---------------------------------------------------------------------------
# C —— 孤儿复活:另一个副本接手,同一个 run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_revived_on_another_replica_keeps_filling_bound_params() -> None:
    script: list[AIMessage | str] = [
        _calls(_BOUND_CALL),
        _HANG,
        _calls(_BOUND_CALL, _EXEC_CALL),
        _DONE,
    ]
    async with _stack(script) as s:
        _thread, r0 = await s.start(mode="queue", inputs=_INPUTS)
        assert await s.worker().run_once() == 1
        await asyncio.wait_for(s.llm.hung.wait(), timeout=5)
        assert s.bound_values() == [_VALUE], "首段在死之前已经用真值派发过一次"
        assert await s.prompt_frame(r0) is not None, "首段的帧已经落库,换副本才读得到"

        # pod-a 「死了」:租约过期,pod-b 的 sweep 接手。pod-b 与 pod-a 只共享检查点和表。
        expired = datetime.now(UTC) - timedelta(seconds=5)
        await s.runs.claim(
            run_id=r0,
            tenant_id=s.tenant_id,
            claimed_by="pod-a",
            lease_until=expired,
            heartbeat_at=expired,
        )
        pod_b = _runtime(s.built, runs=s.runs, events=s.events, instance_id="pod-b")
        sweep = OrphanSweep(
            run_store=s.runs,
            thread_store=s.app.state.thread_meta_repo,
            agent_spec_store=s.app.state.agent_spec_repo,
            runtime=pod_b,
            audit_logger=s.app.state.audit_logger,
            approval_store=s.app.state.approval_store,
        )
        assert await sweep.run_once() == 1
        try:
            assert await s.wait(r0) is RunStatus.SUCCESS
        finally:
            await _cancel_leftover_tasks(pod_b)

        assert s.bound_values() == [_VALUE, _VALUE]
        # 同一个 run:inputs 目录就是它自己的,不需要也不该改指向。
        assert s.sandbox.exec_run_ids == [r0]


# ---------------------------------------------------------------------------
# D —— :regenerate 用被取代那一轮的 inputs;:edit 用请求自己的
# ---------------------------------------------------------------------------


async def _assert_ran_with_the_turn_inputs(s: _Stack, run_id: UUID) -> None:
    frame = await s.prompt_frame(run_id)
    assert frame is not None and frame.get("inputs") == _INPUTS
    docs = _written_docs(s.writer)
    assert docs[inputs_rel_path(run_id)]["variables"][_VARIABLE]["value"] == _VALUE


@pytest.mark.asyncio
async def test_regenerate_in_stream_mode_reuses_the_superseded_turns_inputs() -> None:
    script = [_calls(_BOUND_CALL), _DONE] * 3
    async with _stack(script) as s:
        _thread, r1 = await s.start(inputs=_INPUTS)
        assert await s.wait(r1) is RunStatus.SUCCESS

        resp = await s.post_supersede("regenerate", r1, {"mode": "stream"})
        assert resp.status_code == 200, resp.text
        r2 = UUID(resp.headers["X-Expert-Work-Run-Id"])
        assert await s.wait(r2) is RunStatus.SUCCESS
        await _assert_ran_with_the_turn_inputs(s, r2)

        # 重新生成的那一轮再重新生成一次:它自己的帧记着带过来的 inputs。
        resp = await s.post_supersede("regenerate", r2, {"mode": "stream"})
        assert resp.status_code == 200, resp.text
        r3 = UUID(resp.headers["X-Expert-Work-Run-Id"])
        assert await s.wait(r3) is RunStatus.SUCCESS
        await _assert_ran_with_the_turn_inputs(s, r3)

        assert s.bound_values() == [_VALUE, _VALUE, _VALUE]


@pytest.mark.asyncio
async def test_regenerate_in_queue_mode_carries_the_inputs_through_the_queue() -> None:
    script = [_calls(_BOUND_CALL), _DONE] * 2
    async with _stack(script) as s:
        _thread, r1 = await s.start(inputs=_INPUTS)
        assert await s.wait(r1) is RunStatus.SUCCESS

        resp = await s.post_supersede("regenerate", r1, {"mode": "queue"})
        assert resp.status_code == 202, resp.text
        r2 = UUID(resp.json()["data"]["run_id"])
        row = await s.runs.get(run_id=r2, tenant_id=s.tenant_id)
        assert row is not None and (row.enqueued_input or {}).get("inputs") == _INPUTS

        assert await s.worker().run_once() == 1
        assert await s.wait(r2) is RunStatus.SUCCESS
        await _assert_ran_with_the_turn_inputs(s, r2)
        assert s.bound_values() == [_VALUE, _VALUE]


@pytest.mark.asyncio
async def test_regenerating_a_turn_that_went_through_approval_uses_its_first_runs_inputs() -> None:
    """目标是续跑段(没有 ``system_prompt`` 帧):inputs 要沿审批链找回这一轮第一个 run。"""
    script = [_calls(_BOUND_CALL), _DONE, _calls(_BOUND_CALL)]
    async with _stack(script, gated=frozenset({_WIRE})) as s:
        _thread, r0 = await s.start(inputs=_INPUTS)
        assert await s.wait(r0) is RunStatus.PAUSED
        c1 = await s.decide(r0)
        assert await s.wait(c1) is RunStatus.SUCCESS

        resp = await s.post_supersede("regenerate", c1, {"mode": "stream"})
        assert resp.status_code == 200, resp.text
        r2 = UUID(resp.headers["X-Expert-Work-Run-Id"])
        # 新一轮在同一个审批门前停下 —— 审批请求里是填好的真值。
        assert await s.wait(r2) is RunStatus.PAUSED
        await _assert_ran_with_the_turn_inputs(s, r2)
        pending = await s.app.state.approval_store.get_by_run(run_id=r2, tenant_id=s.tenant_id)
        assert pending is not None
        assert pending.proposed_args.get("project_code") == _VALUE


@pytest.mark.asyncio
async def test_edit_keeps_using_the_requests_own_inputs() -> None:
    async with _stack([_DONE, _DONE]) as s:
        _thread, r1 = await s.start(inputs=_INPUTS)
        assert await s.wait(r1) is RunStatus.SUCCESS

        # :edit 不继承旧轮 —— 必填变量没给,仍是 422。
        resp = await s.post_supersede("edit", r1, {"input": "换一个", "mode": "queue"})
        assert resp.status_code == 422, resp.text

        other = {_VARIABLE: "PRJ-other"}
        resp = await s.post_supersede(
            "edit", r1, {"input": "换一个", "mode": "queue", "inputs": other}
        )
        assert resp.status_code == 202, resp.text
        row = await s.runs.get(run_id=UUID(resp.json()["data"]["run_id"]), tenant_id=s.tenant_id)
        assert row is not None and (row.enqueued_input or {}).get("inputs") == other


# ---------------------------------------------------------------------------
# F —— 非 jinja agent:什么都不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_jinja_agent_resumes_exactly_as_before() -> None:
    script = [_calls(_PROBE_CALL), _calls(_EXEC_CALL), _DONE]
    async with _stack(script, jinja=False, gated=frozenset({"probe"})) as s:
        _thread, r0 = await s.start()
        assert await s.wait(r0) is RunStatus.PAUSED
        c1 = await s.decide(r0)
        assert await s.wait(c1) is RunStatus.SUCCESS

        (ctx,) = s.probe.seen
        assert ctx.run_id == c1
        assert ctx.inputs_run_id is None, "没有 inputs 的 run 不该多出 inputs_run_id"
        assert dict(ctx.prompt_inputs or {}) == {}
        assert s.sandbox.exec_run_ids == [c1]
        assert s.writer.execs == []
        frame = await s.prompt_frame(r0)
        assert frame is not None and "inputs" not in frame

        resp = await s.post_supersede("regenerate", c1, {"mode": "queue"})
        assert resp.status_code == 202, resp.text
        row = await s.runs.get(run_id=UUID(resp.json()["data"]["run_id"]), tenant_id=s.tenant_id)
        assert row is not None and set(row.enqueued_input or {}) == {"replay_messages"}
