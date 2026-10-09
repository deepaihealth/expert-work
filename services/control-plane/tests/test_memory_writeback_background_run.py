"""B-168 B2 —— 后台模式下的一轮,从 ``run_agent`` 到 worker 写完记忆。

真图(``build_react_graph``)+ 真检查点 + 真 ``run_agent``(SSE 发帧、落 ``run_event``、
写 run 状态),写回节点在后台模式下只落一行任务;随后由真 worker + 真处理器把它写完。

* (a) 记忆写回挂 30 秒时,``end`` 帧与 ``success`` 不等它。
* (b) ``run_event`` 里的帧与实时流逐帧一致,worker 写完之后也不多一帧。
* (c) 任务里的指针读回的是本轮结束时的完整对话(含最后的回答),不是下一轮之后的。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from control_plane.memory import MemoryWritebackWorker
from control_plane.memory.writeback_processor import MemoryWritebackProcessor
from control_plane.transcript import read_messages
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.persistence.memory import (
    InMemoryMemoryStore,
    InMemoryMemoryWritebackDLQ,
    InMemoryMemoryWritebackJobStore,
    MemoryWritebackJob,
)
from expert_work.protocol import AgentSpec, ModelSpec, Plan, PlanStep
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunManager,
    RunStatus,
)
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, is_end
from orchestrator import (
    GraphRunner,
    MemoryEnv,
    ToolRegistry,
    build_react_graph,
    make_memory_writeback_node,
    make_reflect_node,
)
from orchestrator.graph_builder.platform_context import PLATFORM_CONTEXT_OPEN
from orchestrator.llm import FakeEmbedder
from orchestrator.sse import _BACKGROUND_PERSIST_WRITERS, run_agent
from orchestrator.tools.registry import ToolSpec

#: 写回在 run 里要花的时间 —— 后台模式下 run 根本不该等它。
_SLOW_WRITEBACK_S = 30.0
#: run 从开始到 ``end`` 的上限:远小于 ``_SLOW_WRITEBACK_S``,又给慢机器留足余量。
_RUN_BUDGET_S = 5.0

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "mem-agent", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "glm", "name": "glm-5.3"},
        "system_prompt": {"template": "you are a test agent"},
        "memory": {"long_term": {"retrieve_top_k": 5, "write_back": True}},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}

_EXTRACTED = (
    '{"memories": [{"kind": "fact", "content": "likes green tea", '
    '"importance": 0.9, "confidence": 0.9}]}'
)


class _AgentLLM:
    """主模型:每轮回一句固定的话(带上用户这句,好认出是哪一轮)。"""

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        on_delta: Any = None,
        **_: Any,
    ) -> AIMessage:
        del tools, on_delta
        last_human = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
        # The platform context (plan etc.) is appended to the last message per call.
        said = str(last_human.content).split(PLATFORM_CONTEXT_OPEN, 1)[0].strip()
        return AIMessage(content=f"final answer to: {said}")


class _SlowExtractor:
    """run 内的记忆模型:一调就挂 ``_SLOW_WRITEBACK_S`` 秒(inline 模式下会挡住 ``end``)。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        await asyncio.sleep(_SLOW_WRITEBACK_S)
        return AIMessage(content=_EXTRACTED)


class _RecordingExtractor:
    """worker 里的记忆模型:记下喂进来的对话,立刻回。"""

    def __init__(self) -> None:
        self.prompts: list[list[BaseMessage]] = []

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        return AIMessage(content=_EXTRACTED)


class _Wakes:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


async def _await_persist_writers() -> None:
    pending = list(_BACKGROUND_PERSIST_WRITERS)
    if pending:
        await asyncio.gather(*pending)


class _AcceptingReflector:
    """复查模型:一律通过,本轮从 ``reflect`` 出口走到写回。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        return AIMessage(content='{"verdict": "accept", "critique": "fine"}')


class _Harness:
    def __init__(
        self, checkpointer: BaseCheckpointSaver[Any], *, reflect: _AcceptingReflector | None = None
    ) -> None:
        self.tenant, self.user, self.thread = uuid4(), uuid4(), uuid4()
        self.checkpointer = checkpointer
        self.memory = InMemoryMemoryStore()
        self.embedder = FakeEmbedder(dim=8)
        self.dlq = InMemoryMemoryWritebackDLQ()
        self.jobs = InMemoryMemoryWritebackJobStore()
        self.agents = InMemoryAgentSpecStore()
        self.slow = _SlowExtractor()
        self.extractor = _RecordingExtractor()
        self.wakes = _Wakes()
        self.bridge = InMemoryStreamBridge()
        self.run_store = InMemoryRunStore()
        self.run_manager = RunManager(store=self.run_store)
        self.events = InMemoryRunEventStore()
        node = make_memory_writeback_node(
            memory_store=self.memory,
            embedder=self.embedder,
            llm_caller=self.slow,
            dlq=self.dlq,
            agent_name="mem-agent",
            agent_version="1.0.0",
            writeback_jobs=self.jobs,
            wake_writeback=self.wakes,
        )
        self.graph = GraphRunner(checkpointer=checkpointer).compile(
            build_react_graph(
                llm_caller=_AgentLLM(),
                tool_registry=ToolRegistry(),
                memory_writeback_node=node,
                reflect_node=make_reflect_node(reflect, budget=1) if reflect is not None else None,
            )
        )

    async def add_agent(self) -> None:
        await self.agents.create(
            tenant_id=self.tenant,
            spec=AgentSpec.model_validate(deepcopy(_SPEC)),
            spec_sha256="0" * 64,
            created_by="t",
        )

    async def run_turn(self, text: str, *, plan: Plan | None = None) -> UUID:
        """一轮:``run_agent`` 跑完(带时限)就返回 run id。"""
        record = await self.run_manager.create(
            run_id=uuid4(), thread_id=self.thread, tenant_id=self.tenant, user_id=self.user
        )
        await asyncio.wait_for(
            run_agent(
                bridge=self.bridge,
                run_manager=self.run_manager,
                record=record,
                graph=self.graph,
                graph_input={
                    "messages": [SystemMessage(content="help"), HumanMessage(content=text)],
                    "step_count": 0,
                    "max_steps": 5,
                    **({"plan": plan} if plan is not None else {}),
                },
                config={
                    "configurable": {
                        "thread_id": str(self.thread),
                        "tenant_id": str(self.tenant),
                        "user_id": str(self.user),
                        "run_id": str(record.run_id),
                    }
                },
                event_store=self.events,
            ),
            timeout=_RUN_BUDGET_S,
        )
        return record.run_id

    async def live_frames(self, run_id: UUID) -> list[tuple[str, Any]]:
        frames: list[tuple[str, Any]] = []
        stream: AsyncIterator[Any] = self.bridge.subscribe(run_id, heartbeat_interval=5.0)
        async for entry in stream:
            if is_end(entry):
                break
            frames.append((entry.event, entry.data))
        return frames

    async def stored_frames(self, run_id: UUID) -> list[tuple[str, Any]]:
        await _await_persist_writers()
        rows = await self.events.list(run_id=run_id, limit=1000)
        return [(r.event_name, r.data) for r in rows]

    def worker(self) -> MemoryWritebackWorker:
        async def factory(spec: AgentSpec, model: ModelSpec, tenant_id: UUID) -> Any:
            del spec, model, tenant_id
            return self.extractor

        return MemoryWritebackWorker(
            store=self.jobs,
            processor=MemoryWritebackProcessor(
                agent_specs=self.agents,
                memory_env=MemoryEnv(store=self.memory, embedder=self.embedder, dlq=self.dlq),
                checkpointer=self.checkpointer,
                caller_factory=factory,
                usage_store=None,
            ),
        )

    async def job_for(self, run_id: UUID) -> MemoryWritebackJob:
        job = await self.jobs.get_by_run(tenant_id=self.tenant, run_id=run_id)
        assert job is not None, "the writeback node did not enqueue a job"
        return job


def _updates_for(frames: list[tuple[str, Any]], node: str) -> list[dict[str, Any]]:
    return [
        data[node]
        for event, data in frames
        if event == "updates" and isinstance(data, dict) and node in data
    ]


@pytest.mark.asyncio
async def test_end_and_success_do_not_wait_for_a_slow_memory_writeback() -> None:
    """(a) —— 后台模式:写回要 30 秒,``end`` 与 ``success`` 照样马上到。"""
    async with make_checkpointer("memory") as cp:
        h = _Harness(cp)
        run_id = await h.run_turn("I like green tea")

        row = await h.run_store.get(run_id=run_id, tenant_id=h.tenant)
        assert row is not None and row.status is RunStatus.SUCCESS
        frames = await h.live_frames(run_id)  # 读到 ``end`` 才返回
        assert h.slow.calls == 0, "the run must not call the memory model itself"
        [raw] = _updates_for(frames, "memory_writeback")
        # ``_duration_ms`` is the builder's per-node timing stamp.
        update = {k: v for k, v in raw.items() if k != "_duration_ms"}
        assert update == {
            "written_memory_count": 0,
            "memory_writeback_failed": False,
            "memory_writeback_queued": True,
        }
        job = await h.job_for(run_id)
        assert job.status == "pending"
        assert (job.tenant_id, job.user_id, job.thread_id) == (h.tenant, h.user, h.thread)
        assert (job.agent_name, job.agent_version) == ("mem-agent", "1.0.0")
        assert h.wakes.count == 1


@pytest.mark.asyncio
async def test_event_replay_matches_the_live_stream_even_after_the_worker_runs() -> None:
    """(b) —— 落库的帧与实时流逐帧一致;worker 写完记忆之后也不多出一帧。"""
    async with make_checkpointer("memory") as cp:
        h = _Harness(cp)
        await h.add_agent()
        run_id = await h.run_turn("I like green tea")
        live = await h.live_frames(run_id)

        assert await h.worker().run_once() is True
        assert (await h.job_for(run_id)).status == "done"

        stored = await h.stored_frames(run_id)
        assert stored == live
        # 写回节点那一帧是最后一个 ``updates``,值是「已排队」。
        last_update = next(data for event, data in reversed(stored) if event == "updates")
        assert last_update["memory_writeback"]["memory_writeback_queued"] is True


@pytest.mark.asyncio
async def test_job_pointer_reads_the_turn_end_conversation() -> None:
    """(c) —— 指针读回本轮结束时的完整对话:有本轮最后的回答,没有下一轮。"""
    async with make_checkpointer("memory") as cp:
        h = _Harness(cp)
        await h.add_agent()
        first = await h.run_turn("I like green tea")
        await h.run_turn("and I hate coffee")  # 下一轮已经跑完,worker 才来
        job = await h.job_for(first)

        assert job.checkpoint_id is not None
        turn_end = await read_messages(cp, h.thread, checkpoint_id=job.checkpoint_id)
        assert [type(m).__name__ for m in turn_end] == [
            "SystemMessage",
            "HumanMessage",
            "AIMessage",
        ]
        assert turn_end[-1].content == "final answer to: I like green tea"
        assert job.message_count == len(turn_end)

        # The real worker + processor read the same pointer.
        worker = h.worker()
        assert await worker.run_once() is True
        first_prompt = "\n".join(str(m.content) for m in h.extractor.prompts[0])
        assert "final answer to: I like green tea" in first_prompt
        assert "coffee" not in first_prompt
        [item] = await h.memory.list_for_user(tenant_id=h.tenant, user_id=h.user)
        assert item.content == "likes green tea"
        assert item.source_run_id == str(first)


def _nodes_in_order(frames: list[tuple[str, Any]]) -> list[str]:
    return [
        node
        for event, data in frames
        if event == "updates" and isinstance(data, dict)
        for node in data
    ]


async def _assert_pointer_reads_the_final_answer(h: _Harness, run_id: UUID, text: str) -> None:
    job = await h.job_for(run_id)
    assert job.checkpoint_id is not None
    turn_end = await read_messages(h.checkpointer, h.thread, checkpoint_id=job.checkpoint_id)
    assert turn_end[-1].content == f"final answer to: {text}"
    assert job.message_count == len(turn_end)


@pytest.mark.asyncio
async def test_pointer_reads_the_final_answer_after_a_reflect_exit() -> None:
    """(c') —— 有复查时,写回前最后一步是 ``reflect``,不是 ``agent``。"""
    async with make_checkpointer("memory") as cp:
        reflector = _AcceptingReflector()
        h = _Harness(cp, reflect=reflector)
        run_id = await h.run_turn("I like green tea")

        nodes = _nodes_in_order(await h.live_frames(run_id))
        assert nodes[-2:] == ["reflect", "memory_writeback"]
        assert reflector.calls == 1
        await _assert_pointer_reads_the_final_answer(h, run_id, "I like green tea")


@pytest.mark.asyncio
async def test_pointer_reads_the_final_answer_after_a_plan_close_exit() -> None:
    """(c'') —— 计划里还有没完成的步骤时,写回前最后一步是 ``plan_close``。"""
    async with make_checkpointer("memory") as cp:
        h = _Harness(cp)
        plan = Plan(
            goal="answer", steps=(PlanStep(id="1", description="answer", status="pending"),)
        )
        run_id = await h.run_turn("I like green tea", plan=plan)

        nodes = _nodes_in_order(await h.live_frames(run_id))
        assert nodes[-2:] == ["plan_close", "memory_writeback"]
        await _assert_pointer_reads_the_final_answer(h, run_id, "I like green tea")
