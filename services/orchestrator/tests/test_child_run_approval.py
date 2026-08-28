"""B-35 PR-4 —— 子 run 撞审批闸 = 软拒(现状 bug 修复).

spec: docs/superpowers/specs/2026-08-28-plan-first-execution-design.md §5。

现状 bug:worker 继承父 ``approval_required_tools``(合成不剥)且
``ask_for_approval`` 无条件注册;worker 内触发审批闸 → 子图写
``pending_approval`` 路由 END → ``run_child_to_result`` 只识别
成功/MaxSteps/Cancelled 三种出口,把最后一个 values chunk 当
``outcome="success"`` —— 审批被静默吞掉,worker 假完成。

修复 = 第四出口:final state 带 ``pending_approval`` → 返回结构化软拒
ToolResult(审批语义收回主线),end 帧 outcome=approval_blocked,
trajectory outcome=failed。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage

from expert_work.protocol import ApprovalRequest
from orchestrator.tools._child_run import run_child_to_result

from .test_worker_event_bridge import _built, _collecting_ctx, _StreamingGraph


def _pending() -> ApprovalRequest:
    now = datetime.now(UTC)
    return ApprovalRequest(
        request_id="req-1",
        node="tools",
        reason_kind="policy_gate",
        action_summary="send_email to customer X",
        proposed_args={"to": "x@example.com"},
        requested_at=now,
        timeout_at=now,
    )


@pytest.mark.asyncio
async def test_child_pending_approval_soft_refuses_instead_of_fake_success() -> None:
    graph = _StreamingGraph(
        updates=[{"agent": {"messages": [AIMessage(content="about to send")]}}],
        final={
            "messages": [AIMessage(content="about to send")],
            "step_count": 1,
            "pending_approval": _pending(),
        },
    )
    frames: list[dict[str, object]] = []
    result = await run_child_to_result(
        child=_built(graph),
        task="do the thing",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata=None,
    )

    content = str(result.content)
    # 不再把子图最后的 AIMessage 文本当正常答案返回。
    assert content != "about to send"
    assert "[worker halted" in content
    assert "human approval" in content
    assert "send_email to customer X" in content
    assert "main conversation" in content
    assert result.meta.get("worker_approval_blocked") is True


@pytest.mark.asyncio
async def test_child_pending_approval_end_frame_outcome_approval_blocked() -> None:
    graph = _StreamingGraph(
        updates=[],
        final={
            "messages": [AIMessage(content="partial")],
            "step_count": 1,
            "pending_approval": _pending(),
        },
    )
    frames: list[dict[str, object]] = []
    await run_child_to_result(
        child=_built(graph),
        task="t",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata=None,
    )
    ends = [f for f in frames if f.get("kind") == "end"]
    assert len(ends) == 1
    data = ends[0]["data"]
    assert isinstance(data, dict)
    assert data.get("outcome") == "approval_blocked"


@pytest.mark.asyncio
async def test_child_without_pending_approval_returns_final_answer() -> None:
    """无审批闸的子 run 行为不变(现状钉住)。"""
    graph = _StreamingGraph(
        updates=[],
        final={"messages": [AIMessage(content="the answer")], "step_count": 1},
    )
    frames: list[dict[str, object]] = []
    result = await run_child_to_result(
        child=_built(graph),
        task="t",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata=None,
    )
    assert str(result.content) == "the answer"
    assert result.meta.get("worker_approval_blocked") is None
