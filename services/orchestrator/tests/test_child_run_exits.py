"""子 run 的非正常出口 —— 每个出口都要对父如实交代,并给前端一个 end 帧.

``run_child_to_result`` 以前只认三种出口(正常 / ``MaxStepsExceededError`` /
``RunCancelledError``),其余情况各有各的谎:

* run 的墙钟到了,子代却照跑 —— ``deadline_at`` 只在派出去之前查一次;
* 子代撞了步数 / token / 无进展预算,被逼着「收个尾」,父拿到的却是一个普通成功;
* 子代一个 AI 回答都没有,也记成 COMPLETED;
* 子代抛了任何别的异常 —— 没有 end 帧(前端卡片永远「运行中」),也没有轨迹;
* 最后一条回答是 content block 列表时,父拿到的是 ``str(list)``。

end 帧会转发给对接方前端,``outcome`` 只能取文档里已有的四个值
(``docs/api/streaming-events.md``):success / max_steps / cancelled /
approval_blocked。这里不新增取值。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from expert_work.protocol import SubagentStatus
from expert_work.runtime.cancellation import RunCancelledError
from orchestrator.tools import _child_run
from orchestrator.tools._child_run import run_child_to_result
from orchestrator.tools.registry import ToolContext, ToolResult

from .test_worker_event_bridge import _built, _collecting_ctx, _StreamingGraph


@dataclass
class _FakeRecorder:
    records: list[Any] = field(default_factory=list)

    async def record(self, record: Any) -> None:
        self.records.append(record)


async def _drain_trajectories() -> None:
    await asyncio.gather(*list(_child_run._BACKGROUND_TRAJECTORY_TASKS))


def _ends(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in frames if f.get("kind") == "end"]


def _invocation(result: ToolResult) -> Any:
    (inv,) = result.state_updates["subagent_invocations"]
    return inv


async def _run(
    graph: Any,
    ctx: ToolContext,
    *,
    recorder: _FakeRecorder | None = None,
    max_steps: int = 5,
) -> ToolResult:
    return await run_child_to_result(
        child=_built(graph, max_steps=max_steps),
        task="t",
        ctx=ctx,
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=recorder,  # type: ignore[arg-type]
        trajectory_metadata={},
    )


# ---------------------------------------------------------------------------
# B3 —— run 的墙钟到了,正在跑的子代也要停
# ---------------------------------------------------------------------------


@dataclass
class _HangingGraph:
    """吐一步进度后就挂住不动(模拟一次很慢的模型调用),记下自己有没有被取消。"""

    progress: dict[str, Any]
    cancelled: bool = False

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del state, config, stream_mode
        yield ("updates", {"agent": {"messages": self.progress["messages"], "step_count": 3}})
        yield ("values", self.progress)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield ("values", {"messages": [AIMessage(content="never")], "step_count": 99})


@pytest.mark.asyncio
async def test_run_deadline_stops_a_running_child_and_returns_its_partial_work() -> None:
    graph = _HangingGraph(
        progress={
            "messages": [HumanMessage(content="t"), AIMessage(content="half way")],
            "step_count": 3,
        }
    )
    frames: list[dict[str, Any]] = []
    ctx = ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        worker_event_sink=_collecting_ctx(frames).worker_event_sink,
        deadline_at=time.monotonic() + 0.05,
    )
    recorder = _FakeRecorder()

    result = await asyncio.wait_for(_run(graph, ctx, recorder=recorder), timeout=2.0)

    content = str(result.content)
    assert content.startswith("[worker stopped: the run's time limit was reached after 3 steps")
    assert content.endswith("half way")
    assert graph.cancelled is True
    assert result.meta.get("subagent_deadline") is True
    inv = _invocation(result)
    assert inv.status is SubagentStatus.FAILED
    assert "time limit" in (inv.error or "")
    (end,) = _ends(frames)
    # 契约里没有「failed」;到墙钟 = 被平台的一个上限叫停、带回部分结果 —— 与
    # 步数用尽同类,用既有的 max_steps。
    assert end["data"]["outcome"] == "max_steps"
    assert end["data"]["iteration_used"] == 3
    await _drain_trajectories()
    assert [r.outcome for r in recorder.records] == ["cancelled"]


@dataclass
class _EndlessGraph:
    """不停吐进度;记下自己的流有没有被关掉(``finally`` 在 aclose 时执行)。"""

    closed: bool = False

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del state, config, stream_mode
        try:
            step = 0
            while True:
                step += 1
                yield ("updates", {"agent": {"messages": [], "step_count": step}})
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_deadline_hitting_while_we_wait_on_the_sink_still_closes_the_child_stream() -> None:
    """墙钟若在我们等 sink 时到点,取消落在我们自己的循环体里,子代的流挂在一个
    yield 上、没被关 —— 真实 LangGraph 下它的节点任务还在跑。必须显式关掉。"""
    graph = _EndlessGraph()
    frames: list[dict[str, Any]] = []

    async def _slow_sink(frame: dict[str, Any]) -> None:
        if frame.get("kind") == "update":
            await asyncio.sleep(10)
        frames.append(frame)

    ctx = ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        worker_event_sink=_slow_sink,
        deadline_at=time.monotonic() + 0.05,
    )

    result = await asyncio.wait_for(_run(graph, ctx), timeout=2.0)

    assert graph.closed is True
    assert str(result.content).startswith("[worker stopped: the run's time limit was reached")
    (end,) = _ends(frames)
    assert end["data"]["outcome"] == "max_steps"


@pytest.mark.asyncio
async def test_timeout_error_raised_by_the_child_itself_is_not_mistaken_for_the_deadline() -> None:
    """子代内部自己抛的 ``TimeoutError``(比如一次 HTTP 超时)不是 run 的墙钟:走
    「意外异常」那条路 —— end 帧照发、异常原样抛给父。"""
    frames: list[dict[str, Any]] = []
    graph = _StreamingGraph([], {}, raise_with=TimeoutError("upstream timed out"))
    ctx = ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        worker_event_sink=_collecting_ctx(frames).worker_event_sink,
        deadline_at=time.monotonic() + 60.0,
    )

    with pytest.raises(TimeoutError, match="upstream timed out"):
        await _run(graph, ctx)

    (end,) = _ends(frames)
    assert end["data"]["outcome"] == "cancelled"


# ---------------------------------------------------------------------------
# B4 —— 被预算逼着收尾 ≠ 正常完成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_reason", "phrase"),
    [
        ("max_steps", "reached its step limit (48 steps)"),
        ("no_progress", "stopped making progress"),
        ("token_budget", "token budget ran out"),
    ],
)
async def test_budget_wrap_up_is_flagged_not_reported_as_plain_success(
    exit_reason: str, phrase: str
) -> None:
    graph = _StreamingGraph(
        [],
        {
            "messages": [AIMessage(content="here is what I have so far")],
            "step_count": 48,
            "exit_reason": exit_reason,
        },
    )
    frames: list[dict[str, Any]] = []
    recorder = _FakeRecorder()

    result = await _run(graph, _collecting_ctx(frames), recorder=recorder, max_steps=48)

    content = str(result.content)
    first_line, _, rest = content.partition("\n\n")
    assert first_line.startswith("[worker stopped early:")
    assert phrase in first_line
    assert "forced wrap-up" in first_line
    assert rest == "here is what I have so far"
    assert result.meta["subagent_exit_reason"] == exit_reason
    # 内容是能用的部分成果,所以 COMPLETED;是不是「被逼收尾」看 meta / 首行。
    inv = _invocation(result)
    assert inv.status is SubagentStatus.COMPLETED
    assert inv.error is None
    (end,) = _ends(frames)
    assert end["data"]["outcome"] == "max_steps"
    await _drain_trajectories()
    assert [r.outcome for r in recorder.records] == ["max_steps"]


@pytest.mark.asyncio
async def test_natural_text_response_stays_a_plain_success() -> None:
    graph = _StreamingGraph(
        [],
        {
            "messages": [AIMessage(content="the answer")],
            "step_count": 2,
            "exit_reason": "text_response",
        },
    )
    frames: list[dict[str, Any]] = []

    result = await _run(graph, _collecting_ctx(frames))

    assert result.content == "the answer"
    assert "subagent_exit_reason" not in result.meta
    (end,) = _ends(frames)
    assert end["data"]["outcome"] == "success"


@pytest.mark.asyncio
async def test_child_with_no_answer_is_failed_not_completed() -> None:
    graph = _StreamingGraph([], {"messages": [HumanMessage(content="t")], "step_count": 1})
    recorder = _FakeRecorder()

    result = await _run(graph, _collecting_ctx([]), recorder=recorder)

    assert "produced no answer" in str(result.content)
    inv = _invocation(result)
    assert inv.status is SubagentStatus.FAILED
    assert inv.error
    await _drain_trajectories()
    assert [r.outcome for r in recorder.records] == ["failed"]


@pytest.mark.asyncio
async def test_block_list_answer_is_joined_text_not_a_python_repr() -> None:
    answer = AIMessage(
        content=[
            {"type": "thinking", "thinking": "let me think"},
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two."},
        ]
    )
    graph = _StreamingGraph([], {"messages": [answer], "step_count": 1})

    result = await _run(graph, _collecting_ctx([]))

    assert result.content == "Part one. Part two."


# ---------------------------------------------------------------------------
# B5 —— 意外异常:end 帧 + 轨迹照记,异常原样抛给父
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_child_exception_emits_end_frame_then_reraises() -> None:
    frames: list[dict[str, Any]] = []
    recorder = _FakeRecorder()
    graph = _StreamingGraph(
        [{"agent": {"messages": [AIMessage(content="working")], "step_count": 1}}],
        {},
        raise_with=RuntimeError("tool exploded"),
    )

    with pytest.raises(RuntimeError, match="tool exploded"):
        await _run(graph, _collecting_ctx(frames), recorder=recorder)

    (end,) = _ends(frames)
    # 契约里没有「failed」:没带回结果就结束的 worker,最接近的既有取值是 cancelled。
    assert end["data"]["outcome"] == "cancelled"
    await _drain_trajectories()
    assert [r.outcome for r in recorder.records] == ["failed"]


@pytest.mark.asyncio
async def test_run_cancel_still_emits_exactly_one_end_frame() -> None:
    """``RunCancelledError`` 走它原有的那条路,新加的兜底不能再补一个 end 帧。"""
    frames: list[dict[str, Any]] = []
    graph = _StreamingGraph([], {}, raise_with=RunCancelledError())

    with pytest.raises(RunCancelledError):
        await _run(graph, _collecting_ctx(frames))

    assert len(_ends(frames)) == 1


@pytest.mark.asyncio
async def test_task_cancellation_propagates_untouched() -> None:
    """``asyncio.CancelledError``(父 task 被取消)不是 ``Exception``,兜底不碰它。"""
    frames: list[dict[str, Any]] = []
    graph = _StreamingGraph([], {}, raise_with=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _run(graph, _collecting_ctx(frames))

    assert _ends(frames) == []
