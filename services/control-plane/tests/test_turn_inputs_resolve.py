"""``control_plane.turn_inputs`` —— 一轮的原始 inputs 从哪来,以及每个 run 入口有没有取。

整条路径的行为由 ``test_turn_inputs_carry.py`` 端到端钉住;这里钉两件那边不好钉的事:

* **不串轮**:同一个会话里上一轮暂停着、这一轮是新开的,取到的必须是这一轮的值。
  对接方的真实用法就是「一个会话、每轮一个业务对象」,串轮 = 拿别的对象的值去调工具。
* **入口穷举**:每个 ``run_agent(...)`` 调用点都登记了它属于哪一类、传没传该传的值。
  新加一个入口不登记就红 —— 「键只写一处」曾被误当成「值处处都对」,这条就是为此。
"""

from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.turn_inputs import TurnInputs, resolve_turn_inputs
from expert_work.persistence import InMemoryApprovalStore
from expert_work.protocol import ApprovalRecord, ApprovalStatus
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
)
from expert_work.runtime.runs.event_store import make_event_record
from expert_work.runtime.runs.schemas import DisconnectMode
from orchestrator.sse import SYSTEM_PROMPT_EVENT

_TENANT = uuid4()
_THREAD = uuid4()
_T0 = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)


class _Thread:
    """一个会话里依次发生的 run,带着各自的帧与审批单。"""

    def __init__(self) -> None:
        self.runs = InMemoryRunStore()
        self.approvals = InMemoryApprovalStore()
        self.events = InMemoryRunEventStore()
        self._n = 0

    async def run(
        self, status: RunStatus, *, inputs: dict[str, Any] | None = None, fresh: bool = True
    ) -> UUID:
        """建一个 run 行;``fresh`` = 新开一轮(``run_agent`` 会发 ``system_prompt`` 帧)。"""
        run_id = uuid4()
        at = _T0 + timedelta(minutes=self._n)
        self._n += 1
        await self.runs.create(
            RunInfo(
                run_id=run_id,
                tenant_id=_TENANT,
                thread_id=_THREAD,
                user_id=None,
                status=status,
                on_disconnect=DisconnectMode.CONTINUE,
                is_resume=not fresh,
                error=None,
                created_at=at,
                updated_at=at,
                finished_at=None,
            )
        )
        if fresh:
            data: dict[str, Any] = {"text": "sys"}
            if inputs:
                data["inputs"] = inputs
            await self.events.append(
                make_event_record(run_id=run_id, seq=1, event_name=SYSTEM_PROMPT_EVENT, data=data)
            )
        return run_id

    async def approve(self, paused: UUID, continuation: UUID) -> None:
        await self.approvals.create(
            ApprovalRecord(
                id=uuid4(),
                tenant_id=_TENANT,
                run_id=paused,
                thread_id=_THREAD,
                request_id=f"approval:{paused}",
                node="tools",
                reason_kind="policy_gate",
                action_summary="gated",
                proposed_args={},
                requested_at=_T0,
                timeout_at=_T0 + timedelta(hours=24),
                status=ApprovalStatus.PENDING,
            )
        )
        decided = await self.approvals.mark_decided(
            run_id=paused,
            tenant_id=_TENANT,
            status=ApprovalStatus.APPROVED,
            decided_by="tester",
            decided_at=_T0,
            modified_args=None,
            continuation_run_id=continuation,
        )
        assert decided

    async def resolve(self, run_id: UUID) -> TurnInputs:
        return await resolve_turn_inputs(
            run_id=run_id,
            thread_id=_THREAD,
            tenant_id=_TENANT,
            runs=self.runs,
            approvals=self.approvals,
            event_store=self.events,
        )


@pytest.mark.asyncio
async def test_a_continuation_finds_its_turns_first_run_through_the_approval_chain() -> None:
    t = _Thread()
    r0 = await t.run(RunStatus.PAUSED, inputs={"pc": "A"})
    c1 = await t.run(RunStatus.PAUSED, fresh=False)
    c2 = await t.run(RunStatus.RUNNING, fresh=False)
    await t.approve(r0, c1)
    await t.approve(c1, c2)

    for run_id in (r0, c1, c2):
        assert await t.resolve(run_id) == TurnInputs(root_run_id=r0, inputs={"pc": "A"})


@pytest.mark.asyncio
async def test_a_new_turn_after_a_paused_one_never_gets_the_paused_turns_inputs() -> None:
    """上一轮停在审批上,这一轮新开(另一个业务对象):各取各的,续跑段也不串。"""
    t = _Thread()
    a0 = await t.run(RunStatus.PAUSED, inputs={"pc": "A"})
    b0 = await t.run(RunStatus.PAUSED, inputs={"pc": "B"})
    b1 = await t.run(RunStatus.RUNNING, fresh=False)
    await t.approve(b0, b1)

    assert await t.resolve(a0) == TurnInputs(root_run_id=a0, inputs={"pc": "A"})
    assert await t.resolve(b0) == TurnInputs(root_run_id=b0, inputs={"pc": "B"})
    assert await t.resolve(b1) == TurnInputs(root_run_id=b0, inputs={"pc": "B"})


@pytest.mark.asyncio
async def test_a_turn_without_inputs_resolves_to_empty_inputs() -> None:
    t = _Thread()
    r0 = await t.run(RunStatus.SUCCESS)
    assert await t.resolve(r0) == TurnInputs(root_run_id=r0, inputs={})


@pytest.mark.asyncio
async def test_a_missing_prompt_frame_is_logged_by_id_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """帧丢了(H-7 落库失败)或审批链没串上:空 inputs,且留一条只有 id 的 warning。"""
    t = _Thread()
    await t.run(RunStatus.PAUSED, inputs={"pc": "A"})
    orphaned = await t.run(RunStatus.RUNNING, fresh=False)  # 没有审批单指向它

    with caplog.at_level(logging.WARNING, logger="control_plane.turn_inputs"):
        got = await t.resolve(orphaned)

    assert got == TurnInputs(root_run_id=orphaned, inputs={})
    (record,) = caplog.records
    # 整条消息逐字比对:除了两个 id 什么都没有(没有值、没有变量名)。
    assert record.getMessage() == (
        f"turn_inputs.prompt_frame_missing run_id={orphaned} root_run_id={orphaned}"
    )


@pytest.mark.asyncio
async def test_no_store_or_unknown_run_resolves_to_its_own_id_and_nothing_else() -> None:
    t = _Thread()
    r0 = await t.run(RunStatus.SUCCESS, inputs={"pc": "A"})
    unknown = uuid4()
    assert await t.resolve(unknown) == TurnInputs(root_run_id=unknown)
    for runs, events in ((None, t.events), (t.runs, None)):
        got = await resolve_turn_inputs(
            run_id=r0,
            thread_id=_THREAD,
            tenant_id=_TENANT,
            runs=runs,
            approvals=t.approvals,
            event_store=events,
        )
        assert got == TurnInputs(root_run_id=r0)


# ---------------------------------------------------------------------------
# 入口穷举
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[1] / "src" / "control_plane"

#: ``run_agent(...)`` 调用点 → 它属于哪一类。
#:
#: * ``"fresh"`` —— 新开一轮:``prompt_inputs`` 来自请求体(``:regenerate`` 来自被取代
#:   那一轮,所以 ``spawn_run`` 也要调 ``resolve_turn_inputs``)。
#: * ``"resume"`` —— 接着跑某一轮:``graph_input=None``,``prompt_inputs`` 与
#:   ``inputs_run_id`` 都必须是 ``resolve_turn_inputs`` 取回的。
#: * ``"resume_or_replay"`` —— 同 ``resume``,但 ``graph_input`` 是**按状态算出来的**
#:   (B-58):有耐久检查点就续(``None``),没有就拿 ``enqueued_input`` 重放。
#:   所以它的 ``graph_input`` 不是字面 ``None``,断言改成两条:必须传变量,
#:   且函数体里必须真有那条重放分支(见下面的 ``_calls_replay_builder``)。
#: * ``None`` —— 不传 inputs,理由写在行上。
_RUN_AGENT_SITES: dict[str, tuple[str | None, bool]] = {
    "api/runs.py::spawn_run": ("fresh", True),
    # 排队 run 的 inputs(含 ``:regenerate`` 带过来的)在 ``enqueued_input`` 里,
    # 由 ``spawn_run`` 建行时写好。
    "run_queue_worker.py::_execute": ("fresh", False),
    "api/runs.py::resolve_approval_decision": ("resume", True),
    "orphan_sweep.py::_respawn": ("resume_or_replay", True),
    # 触发器每次开新会话;触发配置里没有 inputs 这个概念(``seed_input`` 是文本),
    # 这一轮就是没有 inputs —— 它暂停后的续跑取回的也是空的,两段一致。
    "trigger_firing.py::fire_trigger": (None, False),
}


def _enclosing(tree: ast.Module, line: int) -> str:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end and (best is None or node.lineno > best.lineno):
                best = node
    return best.name if best is not None else "<module>"


def _run_agent_calls() -> dict[str, tuple[ast.Call, ast.AST]]:
    found: dict[str, tuple[ast.Call, ast.AST]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_agent"
            ):
                site = f"{path.relative_to(_SRC).as_posix()}::{_enclosing(tree, node.lineno)}"
                assert site not in found, f"{site}: 同一个函数里调了两次 run_agent,拆开登记"
                func = next(
                    f
                    for f in ast.walk(tree)
                    if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
                    and f.name == _enclosing(tree, node.lineno)
                )
                found[site] = (node, func)
    return found


def _calls_replay_builder(func: ast.AST) -> bool:
    """函数体里有没有真的去构建重放输入(B-58 的那条分支)。

    只断言 ``graph_input`` 是个变量太弱 —— 把它改成 ``graph_input = None`` 一行
    也满足,而那正好是本 bug 的样子。
    """
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == "graph_input_from_enqueued"
        for sub in ast.walk(func)
    )


def _calls_resolver(func: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == "resolve_turn_inputs"
        for sub in ast.walk(func)
    )


def test_every_run_agent_call_passes_the_turn_inputs() -> None:
    found = _run_agent_calls()
    assert set(found) == set(_RUN_AGENT_SITES), (
        f"run_agent 的调用点变了,登记进 _RUN_AGENT_SITES 并想清楚它该传哪一轮的 inputs。"
        f" 未登记={sorted(set(found) - set(_RUN_AGENT_SITES))}"
        f" 已消失={sorted(set(_RUN_AGENT_SITES) - set(found))}"
    )
    for site, (kind, resolves) in _RUN_AGENT_SITES.items():
        call, func = found[site]
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        graph_input = kwargs.get("graph_input")
        is_resume = isinstance(graph_input, ast.Constant) and graph_input.value is None
        if kind == "resume_or_replay":
            # B-58 —— 这一档的两条判据都要成立,少一条就是 bug 回来了。
            assert not is_resume, (
                f"{site}: graph_input 又写死成 None 了 —— 没有检查点时 LangGraph 会抛"
                " EmptyInputError(与事实矛盾的「没有输入」),正是 B-58。"
            )
            assert _calls_replay_builder(func), (
                f"{site}: 重放分支不见了 —— graph_input 是变量但没人从 enqueued_input"
                " 构建它,等于只剩「续不了就死」。"
            )
        else:
            assert is_resume == (kind == "resume"), f"{site}: graph_input 与登记的类别对不上"
        if kind is None:
            assert "prompt_inputs" not in kwargs, f"{site}: 开始传 inputs 了,改登记"
            continue
        assert "prompt_inputs" in kwargs, f"{site}: 没传 prompt_inputs"
        assert ("inputs_run_id" in kwargs) == (kind in ("resume", "resume_or_replay")), (
            f"{site}: inputs_run_id 只有续跑类入口该传"
        )
        assert _calls_resolver(func) == resolves, f"{site}: resolve_turn_inputs 的调用与登记不符"
