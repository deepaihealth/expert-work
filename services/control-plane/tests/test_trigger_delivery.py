"""Tests for :func:`control_plane.trigger_delivery.inject_delivery` and
:func:`control_plane.trigger_delivery.deliver_run_result`."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.transcript import read_turns
from control_plane.trigger_delivery import DeliveryOutcome, deliver_run_result, inject_delivery
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.persistence import InMemoryThreadMessageStore, MessageTurn
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec, TriggerRecord
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator.agent_factory import build_agent
from tests.agent_fixtures import stub_agent_runtime

# ---------------------------------------------------------------------------
# Minimal graph build — replicated from
# services/orchestrator/tests/test_agent_factory.py (_MINIMAL_SPEC, _spec(),
# _secret_store(), _platform_resolver). build_agent REQUIRES
# provider_key_resolver (Stream Y-2) or it raises.
# ---------------------------------------------------------------------------

_ANTHROPIC_KEY_NAME = "expert-work/dev/llm/anthropic"
_OPENAI_KEY_NAME = "expert-work/dev/llm/openai"
_KIMI_KEY_NAME = "expert-work/dev/llm/kimi"

_MINIMAL_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "test-agent", "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-6",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
        "system_prompt": {"template": "you are a test agent"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec(**model_overrides: Any) -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["model"].update(model_overrides)
    return AgentSpec.model_validate(doc)


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {
            _ANTHROPIC_KEY_NAME: "sk-ant-test",
            _OPENAI_KEY_NAME: "sk-openai-test",
            _KIMI_KEY_NAME: "sk-kimi-test",
        }
    )


_PROVIDER_KEY_NAMES = {
    "anthropic": _ANTHROPIC_KEY_NAME,
    "openai": _OPENAI_KEY_NAME,
    "kimi": _KIMI_KEY_NAME,
    "self-hosted": _OPENAI_KEY_NAME,
    "azure": _OPENAI_KEY_NAME,
    "qwen": _OPENAI_KEY_NAME,
}


async def _platform_resolver(provider: str) -> list[str]:
    return [f"secret://{_PROVIDER_KEY_NAMES[provider]}"]


async def _built_graph(cp: Any) -> Any:
    spec = _spec()  # a single react agent, no tools needed
    built = await build_agent(
        spec,
        secret_store=_secret_store(),
        checkpointer=cp,
        provider_key_resolver=_platform_resolver,  # required — build_agent raises without it
    )
    return built.graph


@pytest.mark.asyncio
async def test_delivery_appends_ai_message_visible_to_reader() -> None:
    tenant, thread = uuid4(), uuid4()
    async with make_checkpointer("memory") as cp:
        graph = await _built_graph(cp)
        config = {"configurable": {"thread_id": str(thread), "tenant_id": str(tenant)}}
        # seed a prior exchange so the thread has history
        await graph.aupdate_state(
            config,
            {
                "messages": [
                    HumanMessage(content="set up my task"),
                    AIMessage(content="done, it's scheduled"),
                ]
            },
            as_node="agent",
        )
        await inject_delivery(
            graph,
            thread_id=thread,
            tenant_id=tenant,
            result_text="Today's AI news: ...",
            source_run_id=uuid4(),
            trigger_id=uuid4(),
        )
        # the real user-facing read path surfaces it as the last assistant turn
        turns = await read_turns(cp, thread, include_hidden=False)
        assert turns[-1].role == "assistant"
        assert turns[-1].content == "Today's AI news: ..."
        # prove APPEND (not replace): the seeded history must survive
        assert len(turns) == 3
        assert turns[0].content == "set up my task"
        # graph left in a clean turn-complete state (no pending node → next user
        # turn starts fresh, delivery didn't leave the graph mid-execution)
        snap = await graph.aget_state(config)
        assert snap.next == ()


@pytest.mark.asyncio
async def test_delivery_metadata_tags_source() -> None:
    tenant, thread, run_id, trig = uuid4(), uuid4(), uuid4(), uuid4()
    async with make_checkpointer("memory") as cp:
        graph = await _built_graph(cp)
        config = {"configurable": {"thread_id": str(thread), "tenant_id": str(tenant)}}
        await graph.aupdate_state(
            config,
            {"messages": [HumanMessage(content="hi"), AIMessage(content="ok")]},
            as_node="agent",
        )
        await inject_delivery(
            graph,
            thread_id=thread,
            tenant_id=tenant,
            result_text="result",
            source_run_id=run_id,
            trigger_id=trig,
        )
        snap = await graph.aget_state(config)
        last = snap.values["messages"][-1]
        assert last.type == "ai"
        assert last.additional_kwargs["expert_work_scheduled_delivery"] is True
        assert last.additional_kwargs["expert_work_source_run_id"] == str(run_id)
        assert last.additional_kwargs["expert_work_trigger_id"] == str(trig)
        # P2 Task 4 — standard stamp keys, run_id pinned to the source run.
        assert last.additional_kwargs[STAMP_RUN_ID] == str(run_id)
        assert last.additional_kwargs[STAMP_CREATED_AT]
        # NOT hidden from the UI
        assert "expert_work_hide_from_ui" not in last.additional_kwargs


@pytest.mark.asyncio
async def test_delivery_into_empty_thread() -> None:
    """No prior history → as_node='__start__' path still lands the message."""
    tenant, thread = uuid4(), uuid4()
    async with make_checkpointer("memory") as cp:
        graph = await _built_graph(cp)
        await inject_delivery(
            graph,
            thread_id=thread,
            tenant_id=tenant,
            result_text="standalone",
            source_run_id=uuid4(),
            trigger_id=uuid4(),
        )
        turns = await read_turns(cp, thread, include_hidden=False)
        assert any(t.role == "assistant" and t.content == "standalone" for t in turns)


@pytest.mark.asyncio
async def test_inject_delivery_is_idempotent_by_source_run_id() -> None:
    """同一 source_run_id 重复投递只落一条消息(FU1a)——消除 fire-now 端点与
    scheduler reconcile 同进程双投递导致的重复贴。"""
    tenant, thread = uuid4(), uuid4()
    src, trig = uuid4(), uuid4()
    async with make_checkpointer("memory") as cp:
        graph = await _built_graph(cp)
        config = {"configurable": {"thread_id": str(thread), "tenant_id": str(tenant)}}
        await graph.aupdate_state(
            config,
            {
                "messages": [
                    HumanMessage(content="set up my task"),
                    AIMessage(content="done, it's scheduled"),
                ]
            },
            as_node="agent",
        )
        for _ in range(2):
            await inject_delivery(
                graph,
                thread_id=thread,
                tenant_id=tenant,
                result_text="今日 AI 新闻:...",
                source_run_id=src,
                trigger_id=trig,
            )
        turns = await read_turns(cp, thread, include_hidden=True)
        delivered = [t for t in turns if t.content == "今日 AI 新闻:..."]
        assert len(delivered) == 1  # 两次调用,只贴一条


# ---------------------------------------------------------------------------
# deliver_run_result (Spec 1 PR4 Task 2) — extracted from the scheduler's
# former ``_deliver``. Reuses this file's real-graph fixtures above plus the
# stub-runtime / monkeypatch-get_agent pattern PR3's
# ``test_reconcile_delivers_result_to_originating_thread`` (test_scheduler.py)
# established for delivery tests.
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_deliver_run_result_delivers_and_mirrors(monkeypatch: pytest.MonkeyPatch) -> None:
    """reuse_thread 触发器 + SUCCESS run → 结果落原对话 checkpoint,且原对话被
    sync_thread 进搜索镜像(FU2)。"""
    tenant, orig, scratch, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with make_checkpointer("memory") as cp:
        spec = _spec()
        built = await build_agent(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            provider_key_resolver=_platform_resolver,  # required (Stream Y-2)
        )
        # seed: originating conversation has prior history; the run's scratch
        # thread ends with the assistant result to deliver.
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(orig), "tenant_id": str(tenant)}},
            {"messages": [HumanMessage(content="make me a task"), AIMessage(content="scheduled")]},
            as_node="agent",
        )
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(scratch), "tenant_id": str(tenant)}},
            {"messages": [HumanMessage(content="go"), AIMessage(content="Today's AI news: X")]},
            as_node="agent",
        )

        agents = InMemoryAgentSpecStore()
        await agents.create(tenant_id=tenant, spec=spec, spec_sha256="a" * 64, created_by="test")
        runtime = stub_agent_runtime()
        runtime.durable_checkpointer = cp

        async def _get_agent(**_kwargs: Any) -> Any:
            return built

        monkeypatch.setattr(runtime, "get_agent", _get_agent)

        trigger = TriggerRecord(
            id=uuid4(),
            tenant_id=tenant,
            agent_name="test-agent",
            agent_version="1.0.0",
            name="nightly",
            kind="cron",
            config={"expr": "0 9 * * *"},
            enabled=True,
            source="api",
            originating_thread_id=orig,
            context_mode="reuse_thread",
            created_at=_NOW,
            updated_at=_NOW,
        )
        run = RunInfo(
            run_id=run_id,
            tenant_id=tenant,
            thread_id=scratch,
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=_NOW,
            updated_at=_NOW,
            finished_at=_NOW,
        )
        mirror = InMemoryThreadMessageStore()

        outcome = await deliver_run_result(
            trigger=trigger,
            run=run,
            runtime=runtime,
            agent_spec_store=agents,
            thread_message_store=mirror,
            now=_NOW,
        )

        assert outcome == DeliveryOutcome("delivered", text="Today's AI news: X")

        # 原对话 checkpoint 落了结果
        turns = await read_turns(cp, orig, include_hidden=False)
        assert any(t.role == "assistant" and t.content == outcome.text for t in turns)

        # FU2 — the mirror now carries the originating thread's full turn set
        # (including history unique to its pre-existing conversation, not just
        # the newly delivered message) — proof sync_thread(orig, ...) ran.
        assert await mirror.search_thread_ids(tenant_id=tenant, q="make me a task") == {orig}


class _FailingMirrorStore(InMemoryThreadMessageStore):
    """``sync_thread`` always raises — simulates a content-search mirror outage.

    F1 regression fixture: a mirror-sync failure must not downgrade an
    already-succeeded delivery (the message is durably in the originating
    thread's checkpoint by the time FU2 runs).
    """

    async def sync_thread(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        turns: Sequence[MessageTurn],
        synced_at: datetime,
    ) -> None:
        raise RuntimeError("mirror down")


@pytest.mark.asyncio
async def test_deliver_run_result_survives_mirror_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FU2 的镜像同步(sync_thread)失败不得把已成功的投递降级成 error(F1)——
    inject_delivery 此时已把结果落进原对话 checkpoint,search 镜像只是
    best-effort 的副作用,它的故障不该反噬已经成功的投递结果。"""
    tenant, orig, scratch, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with make_checkpointer("memory") as cp:
        spec = _spec()
        built = await build_agent(
            spec,
            secret_store=_secret_store(),
            checkpointer=cp,
            provider_key_resolver=_platform_resolver,  # required (Stream Y-2)
        )
        # seed: originating conversation has prior history; the run's scratch
        # thread ends with the assistant result to deliver.
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(orig), "tenant_id": str(tenant)}},
            {"messages": [HumanMessage(content="make me a task"), AIMessage(content="scheduled")]},
            as_node="agent",
        )
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(scratch), "tenant_id": str(tenant)}},
            {"messages": [HumanMessage(content="go"), AIMessage(content="Today's AI news: X")]},
            as_node="agent",
        )

        agents = InMemoryAgentSpecStore()
        await agents.create(tenant_id=tenant, spec=spec, spec_sha256="a" * 64, created_by="test")
        runtime = stub_agent_runtime()
        runtime.durable_checkpointer = cp

        async def _get_agent(**_kwargs: Any) -> Any:
            return built

        monkeypatch.setattr(runtime, "get_agent", _get_agent)

        trigger = TriggerRecord(
            id=uuid4(),
            tenant_id=tenant,
            agent_name="test-agent",
            agent_version="1.0.0",
            name="nightly",
            kind="cron",
            config={"expr": "0 9 * * *"},
            enabled=True,
            source="api",
            originating_thread_id=orig,
            context_mode="reuse_thread",
            created_at=_NOW,
            updated_at=_NOW,
        )
        run = RunInfo(
            run_id=run_id,
            tenant_id=tenant,
            thread_id=scratch,
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=_NOW,
            updated_at=_NOW,
            finished_at=_NOW,
        )
        mirror = _FailingMirrorStore()

        outcome = await deliver_run_result(
            trigger=trigger,
            run=run,
            runtime=runtime,
            agent_spec_store=agents,
            thread_message_store=mirror,
            now=_NOW,
        )

        # delivery must still report success — the mirror is best-effort
        assert outcome.status == "delivered"
        assert outcome.text == "Today's AI news: X"

        # and inject_delivery's write really landed: the originating thread's
        # checkpoint carries the result regardless of the mirror-sync outcome
        turns = await read_turns(cp, orig, include_hidden=False)
        assert any(t.role == "assistant" and t.content == outcome.text for t in turns)
