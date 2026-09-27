"""B-122 —— worker spec / 提示词 / 构建 env 的工具边界。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.subagent_runtime import (
    _worker_system_prompt,
    make_child_agent_builder,
    make_worker_build_fn,
    synthesize_worker_spec,
)
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec, BuiltinToolSpec
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, ToolEnv
from orchestrator.tools.worker_policy import WORKER_DENIED_BUILTINS

_SANDBOX = {
    "resources": {"cpu": "1.0", "memory": "1Gi"},
    "network": {"egress": "proxy", "allowlist": []},
    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
}

_SHA = "a" * 64


def _parent(tools: list[dict[str, object]]) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "boss", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "deepseek", "name": "deepseek-v4-pro"},
                "system_prompt": {"template": "You are the parent."},
                "sandbox": _SANDBOX,
                "tools": tools,
                "workflow": {"type": "react", "max_iterations": 12},
            },
        }
    )


_DENIED = [{"type": "builtin", "name": n, "config": {}} for n in sorted(WORKER_DENIED_BUILTINS)]
_KEPT = [{"type": "builtin", "name": "web_search", "config": {}}, {"type": "http"}]


def _names(spec: AgentSpec) -> set[str]:
    return {getattr(t, "name", None) or t.type for t in spec.spec.tools}


def test_worker_spec_strips_every_denied_builtin() -> None:
    w = synthesize_worker_spec(
        _parent(_DENIED + _KEPT), role=None, max_iterations=8, allowed_toolsets=[]
    )
    assert _names(w) == {"web_search", "http"}


def test_worker_spec_valve_off_strips_only_manage_task() -> None:
    w = synthesize_worker_spec(
        _parent(_DENIED + _KEPT),
        role=None,
        max_iterations=8,
        allowed_toolsets=[],
        worker_policy=False,
    )
    assert _names(w) == (WORKER_DENIED_BUILTINS - {"manage_task"}) | {"web_search", "http"}


def test_worker_prompt_states_the_boundary() -> None:
    text = synthesize_worker_spec(
        _parent(_KEPT), role="designer", max_iterations=8, allowed_toolsets=[]
    ).spec.system_prompt.template
    assert "deliberately not available to you" in text
    assert "never talk to the end user" in text
    assert "every file you created or modified" in text


def test_worker_prompt_valve_off_is_byte_identical() -> None:
    w = synthesize_worker_spec(
        _parent(_KEPT), role="designer", max_iterations=8, allowed_toolsets=[], worker_policy=False
    )
    assert w.spec.system_prompt.template == _worker_system_prompt("designer")
    assert "deliberately not available" not in w.spec.system_prompt.template
    # Fix round 1 (Minor) — golden anchor copied verbatim from base commit
    # b1c005fd's ``_worker_system_prompt`` (pre-B-122), independent of the
    # function under test: the two lines above would drift together with an
    # accidental edit to the base text, this would not.
    assert w.spec.system_prompt.template.startswith(
        "You are a worker sub-agent spawned to complete a single, focused subtask in isolation."
    )
    assert w.spec.system_prompt.template.endswith(
        "the orchestrator can read the file, and nothing is lost or truncated in the retelling."
    )


# ---------------------------------------------------------------------------
# Build-level — make_worker_build_fn reads the rollback valve once, at
# factory-creation time, and every recursive worker_build_fn call downstream
# carries the same ToolEnv (Review Focus 3).
# ---------------------------------------------------------------------------


@pytest.fixture
def build_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``build_agent`` with a recorder so the wiring is tested
    without real LLM provider clients. Copied verbatim from
    ``test_subagent_runtime.py``'s ``build_calls`` fixture."""
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    return calls


@pytest.mark.asyncio
async def test_worker_build_defaults_to_policy_on(build_calls: list[dict[str, Any]]) -> None:
    """Fix round 1 (Important) — the parent declares every denied builtin, so
    a build that forgets to thread ``worker_policy`` into
    ``synthesize_worker_spec`` (or hardcodes it) is caught here: with
    ``_KEPT``-only parents the spec comprehension is a no-op either way and
    the gap stays invisible."""
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
    )

    await build_fn(_parent(_DENIED + _KEPT), tenant_id=uuid4(), role="probe", depth=1)

    assert build_calls[0]["tool_env"].worker_policy is True
    built_spec = build_calls[0]["spec"]
    assert _names(built_spec) == {"web_search", "http"}
    assert "deliberately not available to you" in built_spec.spec.system_prompt.template


@pytest.mark.asyncio
async def test_worker_build_valve_off_reads_env_at_factory_creation(
    build_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The valve is read once when the factory is created — set it before
    ``make_worker_build_fn`` is called, not before the build call.

    Fix round 1 (Important) — same parent as the default-on test above: the
    rollback shape must leave every denied builtin except ``manage_task`` in
    place and the prompt byte-identical to the unrestricted one, not merely
    leave ``ToolEnv.worker_policy`` flipped while the spec itself still gets
    policed.
    """
    monkeypatch.setenv("EXPERT_WORK_WORKER_TOOL_POLICY", "off")
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
    )

    await build_fn(_parent(_DENIED + _KEPT), tenant_id=uuid4(), role="probe", depth=1)

    assert build_calls[0]["tool_env"].worker_policy is False
    built_spec = build_calls[0]["spec"]
    assert _names(built_spec) == (WORKER_DENIED_BUILTINS - {"manage_task"}) | {
        "web_search",
        "http",
    }
    assert built_spec.spec.system_prompt.template == _worker_system_prompt("probe")


@pytest.mark.asyncio
async def test_grand_worker_build_carries_the_same_policy(
    build_calls: list[dict[str, Any]],
) -> None:
    """A worker that itself spawns a grand-worker reuses ``worker_tool_env``
    (bound once at factory-creation time) through ``tool_env.worker_build_fn``
    — the boundary rides down the recursion, not just the first hop."""
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
    )

    await build_fn(_parent(_KEPT), tenant_id=uuid4(), role="probe", depth=1)
    worker_env = build_calls[0]["tool_env"]
    assert worker_env.worker_build_fn is not None

    await worker_env.worker_build_fn(_parent(_KEPT), tenant_id=uuid4(), role="grandchild", depth=2)

    assert build_calls[1]["tool_env"].worker_policy is True


# ---------------------------------------------------------------------------
# Static sub-agents are NOT workers (Review Focus 4) — B-122 must not reach
# make_child_agent_builder. This guard passes before and after the change.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_child_builder_env_is_not_worker_policed(
    build_calls: list[dict[str, Any]],
) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    spec = _parent(
        [
            {"type": "builtin", "name": "web_search", "config": {}},
            {"type": "builtin", "name": "save_artifact", "config": {}},
        ]
    )
    await store.create(tenant_id=tenant, spec=spec, spec_sha256=_SHA, created_by="test")
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(tenant_id=tenant, name="boss", version="1.0.0", depth=1)

    assert build_calls[0]["tool_env"].worker_policy is False
    assert any(
        isinstance(t, BuiltinToolSpec) and t.name == "save_artifact"
        for t in build_calls[0]["spec"].spec.tools
    )
