"""perf phase2 PR3 T3 — ``AgentRuntime.delegation_gate()`` lifespan wiring.

Mirrors ``test_credential_cache_wiring.py``: drives ``create_app``'s real
lifespan (the ASGI boot path) so a missed ``delegation_config_service=``
wire-up shows up as a broken test, not a silently-ungated gate in prod.
"""

from __future__ import annotations

import pytest

from control_plane.app import create_app
from control_plane.platform_delegation_config import (
    DelegationConfig,
    PlatformDelegationConfigService,
)
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.persistence.platform_delegation_config import (
    InMemoryPlatformDelegationConfigStore,
)
from tests.auth_fixtures import build_test_jwt_verifier


def _service(*, max_concurrent_delegations: int = 5) -> PlatformDelegationConfigService:
    return PlatformDelegationConfigService(
        store=InMemoryPlatformDelegationConfigStore(),
        env_default=DelegationConfig(max_concurrent_delegations=max_concurrent_delegations),
        ttl_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# AgentRuntime.delegation_gate() — unit-level (no app boot)
# ---------------------------------------------------------------------------


def test_delegation_gate_returns_none_without_a_config_service() -> None:
    runtime = AgentRuntime(run_manager=None, stream_bridge=None, agent_builder=None)  # type: ignore[arg-type]
    assert runtime.delegation_gate() is None


def test_delegation_gate_is_a_lazy_singleton() -> None:
    runtime = AgentRuntime(  # type: ignore[arg-type]
        run_manager=None,
        stream_bridge=None,
        agent_builder=None,
        delegation_config_service=_service(),
    )
    gate1 = runtime.delegation_gate()
    gate2 = runtime.delegation_gate()
    assert gate1 is not None
    assert gate1 is gate2  # same object on repeated calls


@pytest.mark.asyncio
async def test_delegation_gate_capacity_follows_the_service() -> None:
    """The gate reads capacity live THROUGH the service (DB-wins-over-env,
    hot for the next acquire) rather than snapshotting it at construction."""
    service = _service(max_concurrent_delegations=1)
    runtime = AgentRuntime(  # type: ignore[arg-type]
        run_manager=None,
        stream_bridge=None,
        agent_builder=None,
        delegation_config_service=service,
    )
    gate = runtime.delegation_gate()
    assert gate is not None

    assert await gate.acquire() is True  # capacity=1, slot taken
    gate._timeout_s = 0.05  # test-only: bound the second acquire's wait
    assert await gate.acquire() is False  # saturated

    await gate.release()
    await service.put(max_concurrent_delegations=2, updated_by="admin")
    assert await gate.acquire() is True
    assert await gate.acquire() is True  # capacity bump picked up live


# ---------------------------------------------------------------------------
# create_app lifespan — real boot path (Step 5 app fixture convention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_wires_delegation_config_service_into_runtime() -> None:
    app = create_app(
        settings=Settings(checkpointer_backend="memory"),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        runtime: AgentRuntime = app.state.agent_runtime
        assert runtime.delegation_config_service is app.state.platform_delegation_config_service
        gate = runtime.delegation_gate()
        assert gate is not None
        # Same singleton across repeated calls once the app is up.
        assert runtime.delegation_gate() is gate
