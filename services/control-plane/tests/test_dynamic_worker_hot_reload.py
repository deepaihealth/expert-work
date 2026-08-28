from __future__ import annotations

from typing import Any

import pytest

from control_plane.platform_dynamic_worker_config import (
    DynamicWorkerConfig,
    PlatformDynamicWorkerConfigService,
)
from control_plane.runtime import AgentRuntime
from control_plane.subagent_runtime import resolve_worker_max_iterations
from expert_work.persistence.platform_dynamic_worker_config import (
    InMemoryPlatformDynamicWorkerConfigStore,
)
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge


async def _stub_agent_builder(
    spec: AgentSpec, *, tenant_id: object = None, user_id: str | None = None
) -> object:
    return object()


def _runtime(**kwargs: Any) -> AgentRuntime:
    """Minimal AgentRuntime construction — mirrors ``test_runtime.py``'s
    shape for the dataclass's required fields (``run_manager`` /
    ``stream_bridge`` / ``agent_builder`` have no defaults)."""
    return AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_stub_agent_builder,  # type: ignore[arg-type]
        **kwargs,
    )


def _service() -> PlatformDynamicWorkerConfigService:
    return PlatformDynamicWorkerConfigService(
        store=InMemoryPlatformDynamicWorkerConfigStore(),
        env_default=DynamicWorkerConfig(
            max_concurrent=3,
            max_per_run=16,
            max_iterations=32,
            cap_max_concurrent=10,
            cap_max_per_run=64,
            cap_max_iterations=128,
        ),
        ttl_seconds=0.0,
    )


def _put_defaults(*, max_concurrent: int, max_per_run: int, max_iterations: int) -> dict[str, Any]:
    """put() kwargs that change the default tier, keeping the env caps."""
    return {
        "max_concurrent": max_concurrent,
        "max_per_run": max_per_run,
        "max_iterations": max_iterations,
        "cap_max_concurrent": 10,
        "cap_max_per_run": 64,
        "cap_max_iterations": 128,
        "updated_by": "admin",
    }


def _parent(**dynamic_workers: int) -> AgentSpec:
    """A minimal parent spec; kwargs become ``dynamic_workers`` budget fields."""
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "boss", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "deepseek", "name": "deepseek-v4-pro"},
                "system_prompt": {"template": "You are the parent."},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
                "tools": [],
                "workflow": {"type": "react", "max_iterations": 64},
                **({"dynamic_workers": dict(dynamic_workers)} if dynamic_workers else {}),
            },
        }
    )


@pytest.mark.asyncio
async def test_spawn_budget_hot_reloads_between_runs() -> None:
    svc = _service()
    runtime = _runtime(dynamic_workers_enabled=True, dynamic_worker_config_service=svc)
    first = await runtime.new_worker_spawn_budget()
    assert (first.max_per_run, first.max_concurrent) == (16, 3)
    await svc.put(**_put_defaults(max_concurrent=5, max_per_run=32, max_iterations=48))
    second = await runtime.new_worker_spawn_budget()
    assert (second.max_per_run, second.max_concurrent) == (32, 5)


@pytest.mark.asyncio
async def test_spawn_budget_falls_back_to_attrs_without_service() -> None:
    runtime = _runtime(
        dynamic_workers_enabled=True,
        dynamic_worker_max_concurrent=2,
        dynamic_worker_max_per_run=8,
    )
    budget = await runtime.new_worker_spawn_budget()
    assert (budget.max_per_run, budget.max_concurrent) == (8, 2)


# ---------------------------------------------------------------------------
# 弹性 worker 预算 — per-agent spawn-budget requests clamped to platform caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_budget_per_agent_request_within_cap() -> None:
    svc = _service()
    runtime = _runtime(dynamic_workers_enabled=True, dynamic_worker_config_service=svc)
    budget = await runtime.new_worker_spawn_budget(
        requested_max_concurrent=5, requested_max_per_run=32
    )
    assert (budget.max_per_run, budget.max_concurrent) == (32, 5)


@pytest.mark.asyncio
async def test_spawn_budget_per_agent_request_clamped_to_cap() -> None:
    svc = _service()
    runtime = _runtime(dynamic_workers_enabled=True, dynamic_worker_config_service=svc)
    budget = await runtime.new_worker_spawn_budget(
        requested_max_concurrent=20, requested_max_per_run=200
    )
    assert (budget.max_per_run, budget.max_concurrent) == (64, 10)


@pytest.mark.asyncio
async def test_spawn_budget_no_service_request_clamped_to_attrs() -> None:
    """service 未接线拿不到 cap → boot attrs 保守攻双角色(default 兼 cap)。"""
    runtime = _runtime(
        dynamic_workers_enabled=True,
        dynamic_worker_max_concurrent=2,
        dynamic_worker_max_per_run=8,
    )
    budget = await runtime.new_worker_spawn_budget(
        requested_max_concurrent=5, requested_max_per_run=32
    )
    assert (budget.max_per_run, budget.max_concurrent) == (8, 2)
    lower = await runtime.new_worker_spawn_budget(
        requested_max_concurrent=1, requested_max_per_run=4
    )
    assert (lower.max_per_run, lower.max_concurrent) == (4, 1)


@pytest.mark.asyncio
async def test_worker_max_iterations_hot_reloads() -> None:
    svc = _service()
    parent = _parent()
    assert await resolve_worker_max_iterations(svc, 32, parent=parent) == 32
    await svc.put(**_put_defaults(max_concurrent=3, max_per_run=16, max_iterations=48))
    assert await resolve_worker_max_iterations(svc, 32, parent=parent) == 48
    assert await resolve_worker_max_iterations(None, 24, parent=parent) == 24
