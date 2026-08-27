"""Unit tests for 1.3 dynamic Orchestrator-Worker — ``spawn_worker`` tool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from expert_work.runtime.cancellation import CancellationToken, RunCancelledError
from orchestrator.agent_factory import BuiltAgent
from orchestrator.errors import MaxStepsExceededError
from orchestrator.tools import ToolBlockedError, ToolContext
from orchestrator.tools.spawn_worker import (
    SPAWN_WORKER_TOOL_NAME,
    SpawnWorkerTool,
    WorkerAgentBuilder,
    WorkerSpawnBudget,
)


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
class _RecordingWorkerBuilder:
    """Conforms to :class:`WorkerAgentBuilder`; records kwargs + returns a
    scripted :class:`BuiltAgent`."""

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
        self.calls.append(
            {"tenant_id": tenant_id, "role": role, "depth": depth, "oauth_user_id": oauth_user_id}
        )
        if self.raises is not None:
            raise self.raises
        assert self.built is not None
        return self.built


def _built(graph: _FakeGraph, *, system_prompt: str = "worker prompt") -> BuiltAgent:
    return BuiltAgent(graph=graph, system_prompt=system_prompt, max_steps=5)  # type: ignore[arg-type]


def _answer_graph(text: str) -> _FakeGraph:
    msgs = [HumanMessage(content="t"), AIMessage(content=text)]
    return _FakeGraph(result={"messages": msgs, "step_count": 2})


def _ctx(*, tenant_id: UUID | None = None, **kw: Any) -> ToolContext:
    return ToolContext(
        tenant_id=uuid4() if tenant_id is None else tenant_id,
        cancellation_token=CancellationToken(),
        **kw,
    )


def _tool(builder: WorkerAgentBuilder, *, child_depth: int = 1) -> SpawnWorkerTool:
    return SpawnWorkerTool(builder=builder, child_depth=child_depth)


# --- tool spec ---------------------------------------------------------------


def test_spec_name_and_params() -> None:
    tool = _tool(_RecordingWorkerBuilder())
    spec = tool.spec
    assert spec.name == SPAWN_WORKER_TOOL_NAME == "spawn_worker"
    assert spec.parameters["required"] == ["task"]
    assert "focus" in spec.parameters["properties"]
    assert spec.is_parallel_safe is True


def test_spec_description_carries_shape_criteria() -> None:
    """委派率增强(层 0)— the description carries the domain-free shape rubric,
    so the delegation judgment lives at the decision site. Pinned phrase by
    phrase so a future rewrite cannot silently drop a criterion."""
    desc = _tool(_RecordingWorkerBuilder()).spec.description
    # Worker profile: fresh context, parallel, cheap.
    assert "sees none of this conversation" in desc
    assert "parallel" in desc
    # Proactive trigger — don't wait for the user to ask for delegation.
    assert "proactively" in desc
    # Positive shape criteria (should-delegate).
    assert "three or more similar, mutually independent sub-items" in desc
    assert "read in full" in desc
    assert "exploratory search" in desc
    # Negative criteria — writes / final decisions stay with the caller.
    assert "Do NOT use" in desc
    assert "final decision" in desc
    # Self-contained task contract + verify-results clause.
    assert "self-contained" in desc
    assert "verify" in desc


# --- happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_builds_worker_and_returns_final_answer() -> None:
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("worker done")))
    tool = _tool(builder, child_depth=2)
    result = await tool.call({"task": "summarize X", "focus": "researcher"}, ctx=_ctx())
    assert result.content == "worker done"
    # focus → role; depth passed through to the builder.
    assert builder.calls[0]["role"] == "researcher"
    assert builder.calls[0]["depth"] == 2
    assert result.meta["dynamic"] is True
    assert result.meta["role"] == "researcher"
    # the task is seeded as the worker's HumanMessage.
    state, _cfg = builder.built.graph.calls[0]  # type: ignore[union-attr]
    assert isinstance(state["messages"][0], SystemMessage)
    assert state["messages"][1].content == "summarize X"


@pytest.mark.asyncio
async def test_focus_omitted_means_general_role() -> None:
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    result = await _tool(builder).call({"task": "do it"}, ctx=_ctx())
    assert builder.calls[0]["role"] is None
    assert result.meta["role"] is None


# --- guards ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tenant_raises_blocked() -> None:
    tool = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("x"))))
    with pytest.raises(ToolBlockedError):
        await tool.call({"task": "x"}, ctx=ToolContext(tenant_id=None))


@pytest.mark.asyncio
async def test_empty_task_raises_value_error() -> None:
    tool = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("x"))))
    with pytest.raises(ValueError, match="non-empty 'task'"):
        await tool.call({"task": "   "}, ctx=_ctx())


@pytest.mark.asyncio
async def test_expired_deadline_declines() -> None:
    tool = _tool(_RecordingWorkerBuilder(built=_built(_answer_graph("x"))))
    with pytest.raises(RunCancelledError):
        await tool.call({"task": "x"}, ctx=_ctx(deadline_at=0.0))


@pytest.mark.asyncio
async def test_max_steps_is_partial_result_not_error() -> None:
    builder = _RecordingWorkerBuilder(
        built=_built(_FakeGraph(raises=MaxStepsExceededError(step_count=5, max_steps=5)))
    )
    result = await _tool(builder).call({"task": "x"}, ctx=_ctx())
    assert "step limit" in result.content
    assert result.meta.get("subagent_max_steps") is True


# --- per-run budget ----------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_blocks_after_max_per_run() -> None:
    budget = WorkerSpawnBudget(max_per_run=2, max_concurrent=4)
    builder = _RecordingWorkerBuilder(built=_built(_answer_graph("ok")))
    tool = _tool(builder)
    ctx = _ctx(worker_spawn_budget=budget)
    r1 = await tool.call({"task": "a"}, ctx=ctx)
    r2 = await tool.call({"task": "b"}, ctx=ctx)
    r3 = await tool.call({"task": "c"}, ctx=ctx)
    assert r1.content == "ok"
    assert r2.content == "ok"
    # third spawn exceeds the per-run cap → soft refusal, builder not called.
    assert r3.meta.get("spawn_worker_blocked") is True
    assert len(builder.calls) == 2


def test_budget_try_reserve_counts() -> None:
    b = WorkerSpawnBudget(max_per_run=2, max_concurrent=1)
    assert b.try_reserve() is True
    assert b.try_reserve() is True
    assert b.try_reserve() is False


@pytest.mark.asyncio
async def test_budget_semaphore_bounds_concurrency() -> None:
    budget = WorkerSpawnBudget(max_per_run=10, max_concurrent=2)
    peak = 0
    live = 0

    async def _slow_ainvoke(state: Any, config: Any) -> Any:
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return {"messages": [AIMessage(content="ok")], "step_count": 1}

    @dataclass
    class _SlowGraph:
        async def ainvoke(self, state: Any, config: Any) -> Any:
            return await _slow_ainvoke(state, config)

        async def astream(
            self, state: Any, config: Any = None, *, stream_mode: Any = None
        ) -> AsyncIterator[Any]:
            del stream_mode
            result = await self.ainvoke(state, config)
            yield ("values", result)

    builder = _RecordingWorkerBuilder(built=_built(_SlowGraph()))  # type: ignore[arg-type]
    tool = _tool(builder)
    ctx = _ctx(worker_spawn_budget=budget)
    await asyncio.gather(*(tool.call({"task": f"t{i}"}, ctx=ctx) for i in range(6)))
    assert peak <= 2


# --- protocol ----------------------------------------------------------------


def test_worker_agent_builder_protocol_accepts_conforming() -> None:
    assert isinstance(_RecordingWorkerBuilder(), WorkerAgentBuilder)


# --- registration gating (build_tool_registry) -------------------------------

from expert_work.protocol import AgentSpec  # noqa: E402
from orchestrator.tools import ToolEnv  # noqa: E402
from orchestrator.tools.assembly import build_tool_registry  # noqa: E402
from orchestrator.tools.subagent import MAX_SUBAGENT_DEPTH  # noqa: E402

_PARENT = AgentSpec.model_validate(
    {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": "boss", "version": "1.0.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "deepseek", "name": "x"},
            "system_prompt": {"template": "hi"},
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": []},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
        },
    }
)


async def _fake_wbf(
    parent_spec: AgentSpec,
    *,
    tenant_id: UUID,
    role: str | None,
    depth: int,
    oauth_user_id: str | None = None,
) -> Any:
    return _built(_answer_graph("ok"))


async def _registry(*, worker_build_fn: Any, enabled: bool = True, depth: int = 0) -> Any:
    parent = _PARENT
    if not enabled:
        data = parent.model_dump(by_alias=True)
        data["spec"]["dynamic_workers"] = {"enabled": False}
        parent = AgentSpec.model_validate(data)
    return await build_tool_registry(
        [],
        tool_env=ToolEnv(worker_build_fn=worker_build_fn),
        parent_spec=parent,
        dynamic_workers=parent.spec.dynamic_workers,
        subagent_depth=depth,
    )


@pytest.mark.asyncio
async def test_registers_spawn_worker_when_wired_and_enabled() -> None:
    reg = await _registry(worker_build_fn=_fake_wbf)
    assert reg.get("spawn_worker") is not None


@pytest.mark.asyncio
async def test_no_spawn_worker_when_builder_unwired() -> None:
    reg = await _registry(worker_build_fn=None)
    assert reg.get("spawn_worker") is None


@pytest.mark.asyncio
async def test_no_spawn_worker_when_opted_out() -> None:
    reg = await _registry(worker_build_fn=_fake_wbf, enabled=False)
    assert reg.get("spawn_worker") is None


@pytest.mark.asyncio
async def test_no_spawn_worker_at_depth_cap() -> None:
    reg = await _registry(worker_build_fn=_fake_wbf, depth=MAX_SUBAGENT_DEPTH)
    assert reg.get("spawn_worker") is None
