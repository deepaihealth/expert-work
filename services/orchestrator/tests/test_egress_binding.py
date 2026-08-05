"""Tests for the egress-binding sandbox runtime (sandbox-egress §3.3).

``_EgressBindingClient`` injects a fixed ``EgressContext`` into every acquire so
sandbox tools carry the agent's egress policy + identity without per-tool
threading.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from orchestrator.tools.sandbox import (
    EgressContext,
    RecordingSandboxRuntime,
    bind_egress,
)

_EGRESS = EgressContext(policy="proxy", agent_name="agent", agent_version="1.0.0")


@pytest.mark.asyncio
async def test_bind_egress_injects_context_into_acquire() -> None:
    inner = RecordingSandboxRuntime()
    client = bind_egress(inner, _EGRESS)

    await client.acquire(tenant_id=uuid4(), thread_id="t-1")

    # The bound context reached the underlying client's acquire — even though
    # the caller passed no egress.
    assert inner.egress_calls == [_EGRESS]


@pytest.mark.asyncio
async def test_bind_egress_overrides_caller_supplied_egress() -> None:
    inner = RecordingSandboxRuntime()
    client = bind_egress(inner, _EGRESS)
    other = EgressContext(policy="none", agent_name="x", agent_version="9")

    await client.acquire(tenant_id=uuid4(), thread_id="t-1", egress=other)

    # The build-time binding wins over anything a caller passes.
    assert inner.egress_calls == [_EGRESS]


@pytest.mark.asyncio
async def test_bind_egress_none_returns_client_unchanged() -> None:
    inner = RecordingSandboxRuntime()
    assert bind_egress(inner, None) is inner


@pytest.mark.asyncio
async def test_binding_client_delegates_other_calls() -> None:
    inner = RecordingSandboxRuntime()
    client = bind_egress(inner, _EGRESS)

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t-1")
    await client.exec(sandbox_id=sandbox_id, code="print(1)", timeout_s=5)
    await client.release(sandbox_id=sandbox_id)
    await client.destroy(sandbox_id=sandbox_id, reason="done")

    assert inner.execs == [(sandbox_id, "print(1)")]
    assert inner.released == [sandbox_id]
    assert inner.destroyed == [(sandbox_id, "done")]
