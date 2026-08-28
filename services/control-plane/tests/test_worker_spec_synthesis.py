"""Unit tests for 1.3 ephemeral worker spec synthesis (control-plane side)."""

from __future__ import annotations

import pytest

from control_plane.platform_dynamic_worker_config import (
    DynamicWorkerConfig,
    PlatformDynamicWorkerConfigService,
)
from control_plane.subagent_runtime import (
    resolve_worker_max_iterations,
    synthesize_worker_spec,
)
from expert_work.protocol import AgentSpec

_SANDBOX = {
    "resources": {"cpu": "1.0", "memory": "1Gi"},
    "network": {"egress": "proxy", "allowlist": []},
    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
}


def _parent(**spec_overrides: object) -> AgentSpec:
    spec = {
        "tenant_config": {},
        "model": {"provider": "deepseek", "name": "deepseek-v4-pro"},
        "system_prompt": {"template": "You are the parent."},
        "sandbox": _SANDBOX,
        "tools": [{"type": "builtin", "name": "web_search", "config": {}}, {"type": "http"}],
        "memory": {"long_term": {"retrieve_top_k": 5}},
        "reflection": {"budget": 2},
        "workflow": {"type": "react", "max_iterations": 12},
        **spec_overrides,
    }
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "boss", "version": "1.0.0", "tenant": "t"},
            "spec": spec,
        }
    )


def test_inherits_model_and_sandbox_strips_state() -> None:
    parent = _parent()
    w = synthesize_worker_spec(parent, role="researcher", max_iterations=8, allowed_toolsets=[])
    # security boundary inherited verbatim
    assert w.spec.model == parent.spec.model
    assert w.spec.sandbox == parent.spec.sandbox
    assert w.spec.tenant_config == parent.spec.tenant_config
    # stateful / delegation blocks stripped (ephemeral, stateless)
    assert w.spec.memory is None
    assert w.spec.reflection is None
    assert w.spec.routing is None
    assert w.spec.subagents == []
    assert w.spec.skills == []
    assert w.spec.triggers == []
    # generated worker prompt carries the role
    assert "researcher" in w.spec.system_prompt.template
    assert w.metadata.name == "boss-worker"


def test_iterations_used_verbatim() -> None:
    """弹性 worker 预算 — ``max_iterations`` arrives already resolved
    (:func:`resolve_worker_max_iterations` owns the clamp policy), so the
    synthesizer applies it verbatim — including above the parent's own cap."""
    parent = _parent(workflow={"type": "react", "max_iterations": 12})
    w = synthesize_worker_spec(parent, role=None, max_iterations=8, allowed_toolsets=[])
    assert w.spec.workflow.max_iterations == 8
    parent2 = _parent(workflow={"type": "react", "max_iterations": 4})
    w2 = synthesize_worker_spec(parent2, role=None, max_iterations=8, allowed_toolsets=[])
    assert w2.spec.workflow.max_iterations == 8


def test_tools_inherited_when_allowlist_empty() -> None:
    w = synthesize_worker_spec(_parent(), role=None, max_iterations=8, allowed_toolsets=[])
    kinds = {getattr(t, "name", None) or getattr(t, "type", None) for t in w.spec.tools}
    assert kinds == {"web_search", "http"}


def test_tools_narrowed_by_allowlist() -> None:
    w = synthesize_worker_spec(
        _parent(), role=None, max_iterations=8, allowed_toolsets=["web_search"]
    )
    kinds = {getattr(t, "name", None) or getattr(t, "type", None) for t in w.spec.tools}
    assert kinds == {"web_search"}


def test_dynamic_workers_stays_enabled_for_recursion() -> None:
    w = synthesize_worker_spec(_parent(), role=None, max_iterations=8, allowed_toolsets=[])
    assert w.spec.dynamic_workers.enabled is True


def test_manage_task_stripped_even_when_allowlist_keeps_it() -> None:
    """BUG-19b —— worker 无 TriggerStore,manage_task 留在合成 spec 里必撞
    build_agent 硬闸(真栈 run 8829abdf 三次 spawn_worker 全败于此)。与剥
    ``triggers:`` 块同一意图:worker 不排任务。空 allowlist(全继承)与显式
    allowlist 点名放行两种路径都必须剥——过滤器放行不等于构建可行。"""
    parent = _parent(
        tools=[
            {"type": "builtin", "name": "web_search", "config": {}},
            {"type": "builtin", "name": "manage_task", "config": {}},
        ]
    )
    w = synthesize_worker_spec(parent, role=None, max_iterations=8, allowed_toolsets=[])
    names = {getattr(t, "name", None) for t in w.spec.tools}
    assert "manage_task" not in names
    assert "web_search" in names

    w2 = synthesize_worker_spec(
        parent, role=None, max_iterations=8, allowed_toolsets=["web_search", "manage_task"]
    )
    names2 = {getattr(t, "name", None) for t in w2.spec.tools}
    assert "manage_task" not in names2


def test_worker_model_override_replaces_model() -> None:
    """``dynamic_workers.model`` 覆盖 worker 的 LLM(全套旋钮),未设则继承
    父 model 原样 —— 便宜档 fan-out 的入口(2026-08-27 用户拍板)。"""
    parent = _parent(
        dynamic_workers={
            "model": {
                "provider": "glm",
                "name": "glm-5.3-flash",
                "effort": "low",
                "max_tokens": 2048,
            }
        }
    )
    w = synthesize_worker_spec(parent, role=None, max_iterations=8, allowed_toolsets=[])
    assert w.spec.model.provider == "glm"
    assert w.spec.model.name == "glm-5.3-flash"
    assert w.spec.model.effort == "low"
    assert w.spec.model.max_tokens == 2048
    # 父 spec 自身不被变异,override 也随 dynamic_workers 继承给孙 worker
    assert parent.spec.model.name == "deepseek-v4-pro"
    assert w.spec.dynamic_workers.model is not None
    # 未配置 → 继承路径不受影响
    w2 = synthesize_worker_spec(_parent(), role=None, max_iterations=8, allowed_toolsets=[])
    assert w2.spec.model == _parent().spec.model


def test_worker_model_override_rejects_fallback() -> None:
    """worker 是短命 fan-out,不配备用链(协议校验器拒绝)。"""
    import pytest

    with pytest.raises(ValueError, match="fallback"):
        _parent(
            dynamic_workers={
                "model": {
                    "provider": "glm",
                    "name": "glm-5.3-flash",
                    "fallback": [{"provider": "deepseek", "name": "deepseek-v4-flash"}],
                }
            }
        )


def test_plan_first_parent_worker_downgraded_to_standard_react() -> None:
    """B-35 —— worker 是聚焦执行体:不跑分发轮(execution_mode 归 standard),
    也不再跑一遍 advisory planner(type 归 react,省一次无意义 LLM 调用)。"""
    parent = _parent(workflow={"type": "plan_execute", "execution_mode": "plan_first"})
    w = synthesize_worker_spec(parent, role=None, max_iterations=8, allowed_toolsets=[])
    assert w.spec.workflow.execution_mode == "standard"
    assert w.spec.workflow.type == "react"


def test_standard_parent_workflow_type_unchanged() -> None:
    """开关关着的父(含存量 plan_execute)workflow.type 原样继承 —— 现状行为钉住。"""
    parent = _parent(workflow={"type": "plan_execute", "max_iterations": 12})
    w = synthesize_worker_spec(parent, role=None, max_iterations=8, allowed_toolsets=[])
    assert w.spec.workflow.type == "plan_execute"
    assert w.spec.workflow.execution_mode == "standard"
    assert w.spec.workflow.max_iterations == 8


# ---------------------------------------------------------------------------
# 弹性 worker 预算 — resolve_worker_max_iterations(clamp policy)
# ---------------------------------------------------------------------------


def _dw_service(
    *, default_iterations: int = 32, cap_iterations: int = 128
) -> PlatformDynamicWorkerConfigService:
    from expert_work.persistence.platform_dynamic_worker_config import (
        InMemoryPlatformDynamicWorkerConfigStore,
    )

    return PlatformDynamicWorkerConfigService(
        store=InMemoryPlatformDynamicWorkerConfigStore(),
        env_default=DynamicWorkerConfig(
            max_concurrent=3,
            max_per_run=16,
            max_iterations=default_iterations,
            cap_max_concurrent=10,
            cap_max_per_run=64,
            cap_max_iterations=cap_iterations,
        ),
        ttl_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_resolve_unrequested_keeps_min_of_parent_and_platform_default() -> None:
    """manifest 未请求 → 现状语义不变:min(父 workflow, 平台默认档)。"""
    svc = _dw_service(default_iterations=8)
    parent = _parent(workflow={"type": "react", "max_iterations": 12})
    assert await resolve_worker_max_iterations(svc, 32, parent=parent) == 8
    parent2 = _parent(workflow={"type": "react", "max_iterations": 4})
    assert await resolve_worker_max_iterations(svc, 32, parent=parent2) == 4


@pytest.mark.asyncio
async def test_resolve_requested_wins_over_parent_workflow() -> None:
    """显式请求赢过「不超过父」启发式,只被平台 cap 压住。"""
    svc = _dw_service(default_iterations=32, cap_iterations=128)
    parent = _parent(
        workflow={"type": "react", "max_iterations": 12},
        dynamic_workers={"max_iterations": 48},
    )
    assert await resolve_worker_max_iterations(svc, 32, parent=parent) == 48


@pytest.mark.asyncio
async def test_resolve_requested_clamped_to_platform_cap() -> None:
    svc = _dw_service(cap_iterations=128)
    parent = _parent(dynamic_workers={"max_iterations": 200})
    assert await resolve_worker_max_iterations(svc, 32, parent=parent) == 128


@pytest.mark.asyncio
async def test_resolve_no_service_requested_clamped_to_boot_fallback() -> None:
    """service 未接线(测试/预热)拿不到 cap → 保守用 boot fallback 当 cap。"""
    parent = _parent(dynamic_workers={"max_iterations": 48})
    assert await resolve_worker_max_iterations(None, 32, parent=parent) == 32
    parent2 = _parent(dynamic_workers={"max_iterations": 6})
    assert await resolve_worker_max_iterations(None, 32, parent=parent2) == 6
