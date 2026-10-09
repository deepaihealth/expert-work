"""B-168 —— ``MemoryWritebackProcessor``:按任务行取配置、读检查点、调记忆模型、写记忆、记账。

模型调用用假 caller(经注入的 caller 工厂),不打真厂商。
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.memory.writeback_processor import MemoryWritebackProcessor
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.persistence.memory import (
    InMemoryMemoryStore,
    InMemoryMemoryWritebackDLQ,
    MemoryWritebackJob,
)
from expert_work.persistence.token_usage_store import (
    NON_BILLABLE_USAGE_KINDS,
    PLATFORM_OVERHEAD_USAGE_KIND,
    InMemoryTokenUsageStore,
)
from expert_work.protocol import AgentSpec, ModelSpec
from orchestrator import MemoryEnv
from orchestrator.agent_factory import memory_model
from orchestrator.llm import FakeEmbedder
from orchestrator.tools.registry import ToolSpec

_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "mem-agent", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        # glm-5.3 has a cheap sibling, so the memory model differs from the main one.
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


def _spec(**memory: Any) -> AgentSpec:
    doc = deepcopy(_SPEC)
    if memory:
        doc["spec"]["memory"] = memory
    return AgentSpec.model_validate(doc)


class _FakeCaller:
    def __init__(self) -> None:
        self.prompts: list[list[BaseMessage]] = []

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        self.prompts.append(list(messages))
        return AIMessage(
            content=_EXTRACTED,
            usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        )


class _Harness:
    def __init__(self) -> None:
        self.tenant, self.user, self.thread, self.run = uuid4(), uuid4(), uuid4(), uuid4()
        self.agents = InMemoryAgentSpecStore()
        self.memory = InMemoryMemoryStore()
        self.dlq = InMemoryMemoryWritebackDLQ()
        self.usage = InMemoryTokenUsageStore()
        self.saver = InMemorySaver()
        self.caller = _FakeCaller()
        self.factory_models: list[ModelSpec] = []
        self.checkpoint_ids: list[str] = []
        self.processor = MemoryWritebackProcessor(
            agent_specs=self.agents,
            memory_env=MemoryEnv(store=self.memory, embedder=FakeEmbedder(dim=8), dlq=self.dlq),
            checkpointer=self.saver,
            caller_factory=self._factory,
            usage_store=self.usage,
        )

    async def _factory(self, spec: AgentSpec, model: ModelSpec, tenant_id: UUID) -> _FakeCaller:
        assert tenant_id == self.tenant
        self.factory_models.append(model)
        return self.caller

    async def add_agent(self, spec: AgentSpec) -> None:
        await self.agents.create(
            tenant_id=self.tenant, spec=spec, spec_sha256="0" * 64, created_by="t"
        )

    async def checkpoint(self, messages: list[BaseMessage]) -> None:
        version = len(self.checkpoint_ids) + 1
        ck = empty_checkpoint()
        ck["channel_values"] = {"messages": messages}
        ck["channel_versions"] = {"messages": version}
        cfg: Any = {"configurable": {"thread_id": str(self.thread), "checkpoint_ns": ""}}
        saved = await self.saver.aput(
            cfg, ck, {"source": "loop", "step": version}, {"messages": version}
        )
        self.checkpoint_ids.append(saved["configurable"]["checkpoint_id"])

    def job(
        self, *, checkpoint_id: str | None, message_count: int | None = None
    ) -> MemoryWritebackJob:
        now = datetime.now(UTC)
        return MemoryWritebackJob(
            id=uuid4(),
            tenant_id=self.tenant,
            user_id=self.user,
            agent_name="mem-agent",
            agent_version="1.0.0",
            thread_id=self.thread,
            checkpoint_id=checkpoint_id,
            message_count=message_count,
            run_id=self.run,
            trace_id=_TRACE,
            status="running",
            attempts=1,
            lease_until=now,
            last_error=None,
            written_count=None,
            failed=None,
            queued_ms=None,
            exec_ms=None,
            created_at=now,
            started_at=now,
            finished_at=None,
        )

    def prompt_text(self) -> str:
        [extract] = self.caller.prompts
        return "\n".join(str(m.content) for m in extract)


async def _yes() -> bool:
    return True


async def _no() -> bool:
    return False


_TURN_1: list[BaseMessage] = [HumanMessage("I like green tea"), AIMessage("noted")]
_TURN_2: list[BaseMessage] = [HumanMessage("and I hate coffee"), AIMessage("ok")]


@pytest.mark.asyncio
async def test_writes_memories_from_the_checkpoint_the_job_points_at() -> None:
    h = _Harness()
    await h.add_agent(_spec())
    await h.checkpoint(_TURN_1)
    await h.checkpoint(_TURN_1 + _TURN_2)  # the next turn already ran

    outcome = await h.processor(h.job(checkpoint_id=h.checkpoint_ids[0]), still_queued=_yes)

    assert (outcome.written_count, outcome.failed, outcome.discarded) == (1, False, False)
    text = h.prompt_text()
    assert "green tea" in text
    assert "coffee" not in text  # read at this turn's end, not the thread's latest
    [item] = await h.memory.list_for_user(tenant_id=h.tenant, user_id=h.user)
    assert item.content == "likes green tea"
    assert item.source_run_id == str(h.run)
    assert item.source_thread_id == str(h.thread)
    # The router is built for the agent's memory model (cheap sibling), as in-run.
    assert h.factory_models == [memory_model(_spec())]
    assert h.factory_models[0].name == "glm-5.3-flash"


@pytest.mark.asyncio
async def test_message_count_truncates_when_there_is_no_checkpoint_id() -> None:
    h = _Harness()
    await h.add_agent(_spec())
    await h.checkpoint(_TURN_1 + _TURN_2)

    await h.processor(h.job(checkpoint_id=None, message_count=2), still_queued=_yes)

    text = h.prompt_text()
    assert "green tea" in text and "coffee" not in text


@pytest.mark.asyncio
async def test_usage_is_platform_overhead_even_outside_a_run() -> None:
    """(e) —— 后台没有 run 的上下文:``charge_in_run`` 在这里静默不记,必须显式按
    任务的租户 / 用户记一行 ``platform_overhead``,挂在原 run 的 trace 上;run 的用量
    (对外只取 ``conversation``,控制台排除平台开销)两处都不含它(§8 问题 3 拍板 (b))。"""
    h = _Harness()
    await h.add_agent(_spec())
    await h.checkpoint(_TURN_1)

    await h.processor(h.job(checkpoint_id=h.checkpoint_ids[0]), still_queued=_yes)

    [row] = h.usage._rows
    assert row.tenant_id == h.tenant
    assert row.user_id == h.user
    assert row.usage_kind == PLATFORM_OVERHEAD_USAGE_KIND
    assert (row.provider, row.model) == ("glm", "glm-5.3-flash")
    assert (row.agent_name, row.agent_version) == ("mem-agent", "1.0.0")
    assert (row.input_tokens, row.output_tokens) == (11, 7)
    assert row.trace_id == _TRACE
    external = await h.usage.totals_by_trace_ids([_TRACE], usage_kinds=("conversation",))
    console = await h.usage.totals_by_trace_ids(
        [_TRACE], exclude_usage_kinds=NON_BILLABLE_USAGE_KINDS
    )
    assert _TRACE not in external
    assert _TRACE not in console


@pytest.mark.asyncio
async def test_a_purged_job_discards_before_writing() -> None:
    """(d) —— 存库前发现任务行没了:不写记忆、不进 DLQ。"""
    h = _Harness()
    await h.add_agent(_spec())
    await h.checkpoint(_TURN_1)

    outcome = await h.processor(h.job(checkpoint_id=h.checkpoint_ids[0]), still_queued=_no)

    assert outcome.discarded is True
    assert await h.memory.list_for_user(tenant_id=h.tenant, user_id=h.user) == []
    assert await h.dlq.count() == 0


@pytest.mark.asyncio
async def test_a_gone_agent_is_done_without_calling_the_model() -> None:
    h = _Harness()
    await h.checkpoint(_TURN_1)

    outcome = await h.processor(h.job(checkpoint_id=h.checkpoint_ids[0]), still_queued=_yes)

    assert (outcome.written_count, outcome.failed, outcome.discarded) == (0, False, False)
    assert outcome.note is not None and "agent" in outcome.note
    assert h.caller.prompts == []


@pytest.mark.asyncio
async def test_memory_turned_off_is_done_without_calling_the_model() -> None:
    h = _Harness()
    await h.add_agent(_spec(long_term={"retrieve_top_k": 5, "write_back": False}))
    await h.checkpoint(_TURN_1)

    outcome = await h.processor(h.job(checkpoint_id=h.checkpoint_ids[0]), still_queued=_yes)

    assert outcome.written_count == 0 and outcome.note is not None
    assert h.caller.prompts == []
