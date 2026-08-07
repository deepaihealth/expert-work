"""Tests for the agent-key-binding sandbox runtime (spec 决策 10).

``_AgentKeyBindingClient`` injects a fixed ``agent_key`` into every exec so
sandbox tools carry ``PYTHONUSERBASE=/opt/agents/<agent_key>`` isolation
without per-tool threading — mirrors ``_EgressBindingClient``
(``test_egress_binding.py``), but binds into ``exec`` instead of ``acquire``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from orchestrator.tools.sandbox import RecordingSandboxRuntime, bind_agent_key


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
async def test_bind_agent_key_overrides_caller_supplied_key() -> None:
    inner = RecordingSandboxRuntime()
    client = bind_agent_key(inner, "my-agent")
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t-1")

    await client.exec(sandbox_id=sandbox_id, code="print(1)", timeout_s=5, agent_key="other")

    # The build-time binding wins over anything a caller passes.
    assert inner.exec_agent_keys == ["my-agent"]


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
