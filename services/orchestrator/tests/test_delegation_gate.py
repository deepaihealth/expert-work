"""Unit tests for 二期 PR3(spec P4)— ``DelegationGate`` process-wide
delegation concurrency gate + its wiring through ``SubAgentTool`` /
``SpawnWorkerTool`` / ``_build_tool_context`` / ``_child_config``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import SubAgentSpec
from expert_work.runtime.cancellation import CancellationToken, RunCancelledError
from orchestrator.agent_factory import BuiltAgent
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.tools._budget import DELEGATION_GATE_KEY, DelegationGate
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.spawn_worker import SpawnWorkerTool, WorkerAgentBuilder
from orchestrator.tools.subagent import SubAgentTool

_SUB = SubAgentSpec(
    name="researcher",
    agent_ref="deep-researcher@1.0.0",
    description="Delegates deep research subtasks.",
)


# ---------------------------------------------------------------------------
# DelegationGate — Step 1
# ---------------------------------------------------------------------------


def _capacity(n: int) -> Any:
    async def _provider() -> int:
        return n

    return _provider


@pytest.mark.asyncio
async def test_third_acquire_waits_until_a_release_frees_a_slot() -> None:
    gate = DelegationGate(_capacity(2), timeout_s=5.0)
    assert await gate.acquire() is True
    assert await gate.acquire() is True
    assert gate.active == 2

    third = asyncio.ensure_future(gate.acquire())
    await asyncio.sleep(0.05)
    assert not third.done()  # still waiting — both slots held

    await gate.release()
    result = await asyncio.wait_for(third, timeout=1.0)
    assert result is True
    assert gate.active == 2  # one released, the waiter took the freed slot


@pytest.mark.asyncio
async def test_acquire_times_out_returns_false_not_raises() -> None:
    gate = DelegationGate(_capacity(1), timeout_s=0.05)
    assert await gate.acquire() is True  # occupies the only slot

    started = time.monotonic()
    result = await gate.acquire()  # never released — must time out, not hang
    elapsed = time.monotonic() - started

    assert result is False
    assert elapsed < 1.0  # bounded by timeout_s, not real 30s


@pytest.mark.asyncio
async def test_nested_acquire_on_saturated_gate_does_not_deadlock() -> None:
    """A depth-1 delegation holding the only slot, attempting a depth-2
    acquire on the SAME process-wide gate, degrades to a soft timeout
    instead of hanging forever waiting for its own release."""
    gate = DelegationGate(_capacity(1), timeout_s=0.05)
    assert await gate.acquire() is True  # depth-1 occupies the slot

    depth2 = await gate.acquire()  # depth-2, same gate, same holder

    assert depth2 is False
    await gate.release()  # depth-1 cleans up
    assert gate.active == 0


@pytest.mark.asyncio
async def test_release_notifies_waiters_promptly() -> None:
    gate = DelegationGate(_capacity(1), timeout_s=5.0)
    assert await gate.acquire() is True

    waiter = asyncio.ensure_future(gate.acquire())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    started = time.monotonic()
    await gate.release()
    result = await asyncio.wait_for(waiter, timeout=1.0)
    elapsed = time.monotonic() - started

    assert result is True
    assert elapsed < 1.0  # notified, not waiting out a poll interval


@pytest.mark.asyncio
async def test_capacity_provider_is_read_live_on_each_acquire() -> None:
    """Config hot-reload semantics: a capacity change applies to the NEXT
    acquire, not to slots already held."""
    capacity = 1

    async def _provider() -> int:
        return capacity

    gate = DelegationGate(_provider, timeout_s=0.05)
    assert await gate.acquire() is True
    assert await gate.acquire() is False  # capacity=1, slot taken

    capacity = 2  # hot bump
    assert await gate.acquire() is True  # next acquire sees the new capacity


@pytest.mark.asyncio
async def test_release_below_zero_clamps_to_zero() -> None:
    gate = DelegationGate(_capacity(2), timeout_s=1.0)
    await gate.release()  # no prior acquire
    assert gate.active == 0


# ---------------------------------------------------------------------------
# FakeGate — records acquire()/release() calls for the tool-level tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeGate:
    admit: bool = True
    acquire_calls: int = 0
    release_calls: int = 0

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        return self.admit

    async def release(self) -> None:
        self.release_calls += 1


# ---------------------------------------------------------------------------
# SubAgentTool.call — delegation gate
# ---------------------------------------------------------------------------


@dataclass
class _FakeGraph:
    result: dict[str, Any] | None = None
    raises: BaseException | None = None
    calls: list[tuple[Any, Any]] = field(default_factory=list)

    async def ainvoke(self, state: Any, config: Any) -> Any:
        self.calls.append((state, config))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del stream_mode
        result = await self.ainvoke(state, config)
        yield ("values", result)


@dataclass
class _RecordingBuilder:
    built: BuiltAgent | None = None
    raises: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> BuiltAgent:
        self.calls.append(
            {"tenant_id": tenant_id, "name": name, "version": version, "depth": depth}
        )
        if self.raises is not None:
            raise self.raises
        assert self.built is not None
        return self.built


@dataclass
class _RecordingWorkerBuilder:
    built: BuiltAgent | None = None
    raises: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        role: str | None,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> BuiltAgent:
        self.calls.append({"tenant_id": tenant_id, "role": role, "depth": depth})
        if self.raises is not None:
            raise self.raises
        assert self.built is not None
        return self.built


def _built(graph: _FakeGraph) -> BuiltAgent:
    return BuiltAgent(graph=graph, system_prompt="child prompt", max_steps=5)  # type: ignore[arg-type]


def _answer_graph(text: str) -> _FakeGraph:
    return _FakeGraph(result={"messages": [HumanMessage(content="t"), AIMessage(content=text)]})


def _ctx(*, tenant_id: UUID | None = None, **kw: Any) -> ToolContext:
    return ToolContext(
        tenant_id=uuid4() if tenant_id is None else tenant_id,
        cancellation_token=CancellationToken(),
        **kw,
    )


@pytest.mark.asyncio
async def test_subagent_call_acquires_and_releases_gate_on_success() -> None:
    gate = _FakeGate(admit=True)
    builder = _RecordingBuilder(built=_built(_answer_graph("ok")))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=gate))

    assert result.content == "ok"
    assert gate.acquire_calls == 1
    assert gate.release_calls == 1
    assert len(builder.calls) == 1


@pytest.mark.asyncio
async def test_subagent_call_gate_refusal_is_soft_fail_and_skips_child_run() -> None:
    gate = _FakeGate(admit=False)
    builder = _RecordingBuilder(built=_built(_answer_graph("ok")))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=gate))

    assert result.meta["delegation_gated"] is True
    assert result.meta["reason"] == "global_gate_timeout"
    assert builder.calls == []  # child never built
    assert gate.acquire_calls == 1
    assert gate.release_calls == 0  # nothing to release — acquire never succeeded


@pytest.mark.asyncio
async def test_subagent_call_releases_gate_when_child_run_raises() -> None:
    gate = _FakeGate(admit=True)
    graph = _FakeGraph(raises=RunCancelledError("child cancelled"))
    builder = _RecordingBuilder(built=_built(graph))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    with pytest.raises(RunCancelledError):
        await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=gate))

    assert gate.acquire_calls == 1
    assert gate.release_calls == 1  # finally still ran


@pytest.mark.asyncio
async def test_subagent_call_releases_gate_when_builder_raises() -> None:
    gate = _FakeGate(admit=True)
    builder = _RecordingBuilder(raises=KeyError("agent_ref not found"))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    with pytest.raises(KeyError):
        await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=gate))

    assert gate.acquire_calls == 1
    assert gate.release_calls == 1


@pytest.mark.asyncio
async def test_subagent_call_delegation_gate_none_behaves_as_before() -> None:
    builder = _RecordingBuilder(built=_built(_answer_graph("ok")))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=None))

    assert result.content == "ok"
    assert "delegation_gated" not in result.meta


# ---------------------------------------------------------------------------
# SpawnWorkerTool.call — delegation gate (layered on the per-run budget)
# ---------------------------------------------------------------------------


def _worker_tool(builder: WorkerAgentBuilder, *, child_depth: int = 1) -> SpawnWorkerTool:
    return SpawnWorkerTool(builder=builder, child_depth=child_depth)


@pytest.mark.asyncio
async def test_spawn_worker_call_acquires_and_releases_gate_on_success() -> None:
    gate = _FakeGate(admit=True)
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    tool = _worker_tool(builder)

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=gate))

    assert result.content == "ok"
    assert gate.acquire_calls == 1
    assert gate.release_calls == 1
    assert len(builder.calls) == 1


@pytest.mark.asyncio
async def test_spawn_worker_call_gate_refusal_is_soft_fail_and_skips_child_run() -> None:
    gate = _FakeGate(admit=False)
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    tool = _worker_tool(builder)

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=gate))

    assert result.meta["delegation_gated"] is True
    assert result.meta["reason"] == "global_gate_timeout"
    assert builder.calls == []
    assert gate.acquire_calls == 1
    assert gate.release_calls == 0


@pytest.mark.asyncio
async def test_spawn_worker_call_releases_gate_when_child_run_raises() -> None:
    gate = _FakeGate(admit=True)
    graph = _FakeGraph(raises=RunCancelledError("child cancelled"))
    builder = _RecordingWorkerBuilder(built=_built(graph))
    tool = _worker_tool(builder)

    with pytest.raises(RunCancelledError):
        await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=gate))

    assert gate.acquire_calls == 1
    assert gate.release_calls == 1


@pytest.mark.asyncio
async def test_spawn_worker_call_delegation_gate_none_behaves_as_before() -> None:
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    tool = _worker_tool(builder)

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=None))

    assert result.content == "ok"
    assert "delegation_gated" not in result.meta


@pytest.mark.asyncio
async def test_spawn_worker_gate_applies_after_per_run_budget_exhausted() -> None:
    """The per-run budget's own soft-fail takes priority — a call already
    refused by the budget never touches the gate at all."""
    from orchestrator.tools.spawn_worker import WorkerSpawnBudget

    gate = _FakeGate(admit=True)
    budget = WorkerSpawnBudget(max_per_run=0, max_concurrent=1)
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    tool = _worker_tool(builder)

    result = await tool.call(
        {"task": "x"}, ctx=_ctx(worker_spawn_budget=budget, delegation_gate=gate)
    )

    assert result.meta.get("spawn_worker_blocked") is True
    assert gate.acquire_calls == 0  # budget refused first — gate never consulted


# ---------------------------------------------------------------------------
# _build_tool_context — reads DELEGATION_GATE_KEY from configurable
# ---------------------------------------------------------------------------


def test_build_tool_context_reads_delegation_gate_from_config() -> None:
    gate = DelegationGate(_capacity(1))
    config: RunnableConfig = {"configurable": {DELEGATION_GATE_KEY: gate}}

    ctx = _build_tool_context(config)

    assert ctx.delegation_gate is gate


def test_build_tool_context_delegation_gate_defaults_none() -> None:
    ctx = _build_tool_context({"configurable": {}})
    assert ctx.delegation_gate is None


# ---------------------------------------------------------------------------
# _child_config — forwards the SAME gate object to a child's config (the
# gate is process-wide, unlike the per-run worker_spawn_budget which is
# deliberately NOT forwarded)
# ---------------------------------------------------------------------------


def test_child_config_forwards_same_delegation_gate_object() -> None:
    gate = DelegationGate(_capacity(1))
    ctx = ToolContext(tenant_id=uuid4(), delegation_gate=gate)

    child_config = _child_config(ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())

    assert child_config["configurable"][DELEGATION_GATE_KEY] is gate


def test_child_config_omits_delegation_gate_when_absent() -> None:
    ctx = ToolContext(tenant_id=uuid4())
    child_config = _child_config(ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())
    assert DELEGATION_GATE_KEY not in child_config["configurable"]
