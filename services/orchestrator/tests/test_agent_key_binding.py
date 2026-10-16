"""Tests for the agent-key-binding sandbox runtime (spec 决策 10).

``_AgentKeyBindingClient`` injects a fixed ``agent_key`` into every exec so
sandbox tools carry ``PYTHONUSERBASE=/opt/agents/<agent_key>`` isolation
without per-tool threading — mirrors ``_EgressBindingClient``
(``test_egress_binding.py``), but binds into ``exec`` instead of ``acquire``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from orchestrator.tools.sandbox import (
    EgressContext,
    RecordingSandboxRuntime,
    bind_agent_key,
    bind_egress,
)


@pytest.mark.asyncio
async def test_bind_agent_key_injects_key_into_exec() -> None:
    inner = RecordingSandboxRuntime()
    client = bind_agent_key(inner, "my-agent")
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t-1")

    await client.exec(sandbox_id=sandbox_id, code="print(1)", timeout_s=5)

    # The bound key reached the underlying client's exec — even though the
    # caller passed no agent_key.
    assert inner.exec_agent_keys == ["my-agent"]


@pytest.mark.asyncio
async def test_bind_agent_key_yields_to_caller_supplied_key() -> None:
    inner = RecordingSandboxRuntime()
    client = bind_agent_key(inner, "my-agent")
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t-1")

    await client.exec(sandbox_id=sandbox_id, code="print(1)", timeout_s=5, agent_key="other")

    # B-124 —— 调用方(run_in_sandbox 传 ctx.agent_key)的 key 优先;绑定的只是兜底。
    # 委派子代的 ctx.agent_key 是父的,旧的「绑定优先」让子代 exec 进了自己的目录。
    assert inner.exec_agent_keys == ["other"]


@pytest.mark.asyncio
async def test_bind_agent_key_empty_returns_client_unchanged() -> None:
    inner = RecordingSandboxRuntime()
    assert bind_agent_key(inner, "") is inner


@pytest.mark.asyncio
async def test_binding_client_delegates_other_calls() -> None:
    inner = RecordingSandboxRuntime()
    client = bind_agent_key(inner, "my-agent")

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t-1")
    await client.exec(sandbox_id=sandbox_id, code="print(1)", timeout_s=5)
    await client.release(sandbox_id=sandbox_id)
    await client.destroy(sandbox_id=sandbox_id, reason="done")

    assert inner.execs == [(sandbox_id, "print(1)")]
    assert inner.released == [sandbox_id]
    assert inner.destroyed == [(sandbox_id, "done")]


@pytest.mark.asyncio
async def test_the_production_wrapper_chain_forwards_per_call_flags() -> None:
    """``agent_factory`` 装的是 ``bind_agent_key(bind_egress(runtime, …), key)``。两层都只
    透传 ``run_id`` / ``has_skills``(它们是每次调用的事实,不属于任何一层);任一层丢掉
    ``has_skills``,绑了技能的 agent 就拿不到 ``EXPERT_WORK_SKILLS_DIR``。"""
    inner = RecordingSandboxRuntime()
    egress = EgressContext(policy="proxy", agent_name="agent", agent_version="1.0.0")
    client = bind_agent_key(bind_egress(inner, egress), "my-agent")
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t-1")
    run_id = uuid4()

    await client.exec(
        sandbox_id=sandbox_id, code="print(1)", timeout_s=5, run_id=run_id, has_skills=True
    )
    await client.exec(sandbox_id=sandbox_id, code="print(1)", timeout_s=5)

    assert inner.exec_has_skills == [True, False]
    assert inner.exec_run_ids == [run_id, None]
