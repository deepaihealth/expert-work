"""B2 — run_child_to_result 的 worker 帧集成测试."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from expert_work.runtime.cancellation import RunCancelledError
from orchestrator.agent_factory import BuiltAgent
from orchestrator.errors import MaxStepsExceededError
from orchestrator.tools._child_run import run_child_to_result
from orchestrator.tools.registry import ToolContext


class _StreamingGraph:
    """吐 updates chunk 再吐最终 values 的脚本图.

    ``values_before_raise`` — 可选:在 ``raise_with`` 之前先吐一个
    ``("values", ...)`` chunk,钉住"较早 superstep 的 values chunk 在
    随后一步抛错时仍留在 ``result`` 里"这条路径(finding 1)。
    """

    def __init__(
        self,
        updates: list[Any],
        final: dict[str, Any],
        raise_with: BaseException | None = None,
        values_before_raise: dict[str, Any] | None = None,
    ) -> None:
        self.updates = updates
        self.final = final
        self.raise_with = raise_with
        self.values_before_raise = values_before_raise
        self.aget_state_calls = 0

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del state, config, stream_mode
        for chunk in self.updates:
            yield ("updates", chunk)
        if self.values_before_raise is not None:
            yield ("values", self.values_before_raise)
        if self.raise_with is not None:
            raise self.raise_with
        yield ("values", self.final)

    async def aget_state(self, config: Any) -> Any:
        del config
        self.aget_state_calls += 1

        @dataclass
        class _Snap:
            values: dict[str, Any]

        return _Snap(values={"messages": [], "step_count": 1})


def _built(graph: Any, *, system_prompt: str = "worker prompt", max_steps: int = 5) -> BuiltAgent:
    # 同 test_spawn_worker.py:64 的 _built 惯例 — 最小 BuiltAgent 构造。
    return BuiltAgent(graph=graph, system_prompt=system_prompt, max_steps=max_steps)


def _collecting_ctx(frames: list[dict[str, Any]], *, run_id: Any = None) -> ToolContext:
    async def _sink(frame: dict[str, Any]) -> None:
        frames.append(frame)

    return ToolContext(
        tenant_id=uuid4(),
        run_id=run_id or uuid4(),
        worker_event_sink=_sink,
        tool_call_id="call-7",
    )


_FINAL = {"messages": [AIMessage(content="done")], "step_count": 2}
_UPDATES = [
    {"agent": {"messages": [AIMessage(content="thinking")], "step_count": 1}},
    {"tools": {"messages": []}},
]


@pytest.mark.asyncio
async def test_frames_start_updates_end_with_monotonic_wseq() -> None:
    frames: list[dict[str, Any]] = []
    result = await run_child_to_result(
        child=_built(_StreamingGraph(_UPDATES, _FINAL)),
        task="do the thing",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:research",
        trajectory_recorder=None,
        trajectory_metadata={},
        extra_meta={"dynamic": True, "role": "research"},
    )
    assert result.content == "done"
    kinds = [f["kind"] for f in frames]
    assert kinds == ["start", "update", "update", "end"]
    assert [f["wseq"] for f in frames] == [0, 1, 2, 3]
    assert frames[0]["parent_tool_call_id"] == "call-7"
    assert frames[0]["parent_worker_id"] is None  # depth=1
    assert frames[0]["data"]["role"] == "research"
    assert frames[1]["data"]["node"] == "agent"
    assert frames[-1]["data"]["outcome"] == "success"
    assert frames[-1]["data"]["iteration_used"] == 2


@pytest.mark.asyncio
async def test_depth2_frames_carry_parent_worker_id() -> None:
    frames: list[dict[str, Any]] = []
    parent_worker_run = uuid4()
    await run_child_to_result(
        child=_built(_StreamingGraph([], _FINAL)),
        task="t",
        ctx=_collecting_ctx(frames, run_id=parent_worker_run),
        child_depth=2,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )
    assert frames[0]["parent_worker_id"] == str(parent_worker_run)
    assert frames[0]["depth"] == 2


@pytest.mark.asyncio
async def test_cancel_emits_end_cancelled_then_reraises() -> None:
    frames: list[dict[str, Any]] = []
    with pytest.raises(RunCancelledError):
        await run_child_to_result(
            child=_built(_StreamingGraph(_UPDATES[:1], _FINAL, raise_with=RunCancelledError())),
            task="t",
            ctx=_collecting_ctx(frames),
            child_depth=1,
            label="spawn_worker",
            agent_ref="dynamic:general",
            trajectory_recorder=None,
            trajectory_metadata={},
        )
    assert frames[-1]["kind"] == "end"
    assert frames[-1]["data"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_max_steps_emits_end_max_steps_partial_result() -> None:
    frames: list[dict[str, Any]] = []
    result = await run_child_to_result(
        child=_built(
            _StreamingGraph(
                _UPDATES[:1], _FINAL, raise_with=MaxStepsExceededError(step_count=8, max_steps=5)
            )
        ),
        task="t",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )
    assert frames[-1]["data"]["outcome"] == "max_steps"
    assert "step limit" in str(result.content)


@pytest.mark.asyncio
async def test_sink_failure_does_not_break_child_run() -> None:
    async def _boom(frame: dict[str, Any]) -> None:
        raise RuntimeError("sink down")

    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), worker_event_sink=_boom)
    result = await run_child_to_result(
        child=_built(_StreamingGraph(_UPDATES, _FINAL)),
        task="t",
        ctx=ctx,
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )
    assert result.content == "done"


@pytest.mark.asyncio
async def test_no_sink_no_frames_still_works() -> None:
    result = await run_child_to_result(
        child=_built(_StreamingGraph(_UPDATES, _FINAL)),
        task="t",
        ctx=ToolContext(tenant_id=uuid4(), run_id=uuid4()),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )
    assert result.content == "done"


@pytest.mark.asyncio
async def test_max_steps_uses_stale_values_chunk_not_fetch_partial() -> None:
    """Finding 1 — 一个更早 superstep 的 values chunk 在随后一步抛
    ``MaxStepsExceededError`` 时仍留在 ``result`` 里。LangGraph 的
    superstep-checkpoint 语义下这个 chunk 就等于 ``aget_state`` 会
    返回的东西,所以是合法 partial —— 代码直接用它,不再兜底调用
    ``_fetch_partial`` / ``aget_state``。"""
    frames: list[dict[str, Any]] = []
    partial_state = {"messages": [AIMessage(content="partial thought")], "step_count": 1}
    graph = _StreamingGraph(
        _UPDATES[:1],
        _FINAL,
        raise_with=MaxStepsExceededError(step_count=8, max_steps=5),
        values_before_raise=partial_state,
    )
    result = await run_child_to_result(
        child=_built(graph),
        task="t",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )
    assert "step limit" in str(result.content)
    assert frames[-1]["kind"] == "end"
    assert frames[-1]["data"]["outcome"] == "max_steps"
    assert frames[-1]["data"]["iteration_used"] == 1
    assert graph.aget_state_calls == 0


@pytest.mark.asyncio
async def test_sink_failure_does_not_swallow_cancel_reraise() -> None:
    """Finding 2 — sink 在 update 帧和 end(cancelled) 帧上都炸,
    ``_emit_worker_frame`` 吞掉 sink 自己的异常(best-effort),但绝不能
    连带吞掉 ``RunCancelledError`` 的 re-raise。"""

    async def _boom(frame: dict[str, Any]) -> None:
        raise RuntimeError("sink down")

    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), worker_event_sink=_boom)
    with pytest.raises(RunCancelledError):
        await run_child_to_result(
            child=_built(_StreamingGraph(_UPDATES[:1], _FINAL, raise_with=RunCancelledError())),
            task="t",
            ctx=ctx,
            child_depth=1,
            label="spawn_worker",
            agent_ref="dynamic:general",
            trajectory_recorder=None,
            trajectory_metadata={},
        )


# ---------------------------------------------------------------------------
# B-42 —— end 帧的 usage_by_model:按子代自己的 (provider, model) 记账
# ---------------------------------------------------------------------------


def _usage_msg(text: str, *, inp: int, out: int, cache_read: int = 0) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "input_token_details": {"cache_read": cache_read, "cache_creation": 0},
            "output_token_details": {"reasoning": 0},
        },
    )


@pytest.mark.asyncio
async def test_end_frame_buckets_usage_under_the_childs_own_model() -> None:
    """worker 的 token 记在 worker **自己**的 (provider, model) 名下。

    父侧只知道主 Agent 的模型;``dynamic_workers.model`` 换过模型的 worker
    烧的 token 若不带模型名回传,前端只能按主 Agent 的费率算 —— 错价。
    桶的和必须等于 ``usage`` 总量:两个字段说的是同一笔账。
    """
    msgs = [
        _usage_msg("a", inp=100, out=10, cache_read=40),
        AIMessage(content="no usage reported"),
        _usage_msg("b", inp=200, out=20, cache_read=60),
    ]
    graph = _StreamingGraph(updates=[], final={"messages": msgs, "step_count": 2})
    child = BuiltAgent(
        graph=graph,  # type: ignore[arg-type]
        system_prompt="p",
        max_steps=5,
        model_provider="zhipu",
        model_name="glm-5.3",
    )
    frames: list[dict[str, Any]] = []
    await run_child_to_result(
        child=child,
        task="t",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )
    end = frames[-1]
    assert end["kind"] == "end"
    usage = end["data"]["usage"]
    buckets = end["data"]["usage_by_model"]
    assert [(b["provider"], b["model"]) for b in buckets] == [("zhipu", "glm-5.3")]
    expected = {"input_tokens": 300, "output_tokens": 30, "total_tokens": 330}
    # 桶的和 == 总量,逐字段。
    for key, want in expected.items():
        assert sum(b[key] for b in buckets) == usage[key] == want, key
    assert sum(b["input_token_details"]["cache_read"] for b in buckets) == 100
    assert usage["input_token_details"]["cache_read"] == 100


@pytest.mark.asyncio
async def test_end_frame_omits_usage_by_model_when_child_model_unknown() -> None:
    """模型未知(老构造路径 / 测试桩)→ 只有 ``usage``,没有 ``usage_by_model``。
    消费者据此退回老算法;绝不编一个空桶列表让「未知」变成「零成本」。"""
    graph = _StreamingGraph(
        updates=[], final={"messages": [_usage_msg("a", inp=1, out=1)], "step_count": 1}
    )
    frames: list[dict[str, Any]] = []
    await run_child_to_result(
        child=_built(graph),
        task="t",
        ctx=_collecting_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )
    end = frames[-1]
    assert end["kind"] == "end"
    assert "usage" in end["data"]
    assert "usage_by_model" not in end["data"]
