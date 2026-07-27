"""二期 PR2 T4 — built-agent cache bounds (LRU + TTL + size gauge).

Covers BOTH built-agent caches:

- ``AgentRuntime._cache`` (top-level builds)
- the ``make_child_agent_builder`` closure cache (delegated sub-agents)

Locked semantics:

- LRU capacity: inserting past ``cache_max_size`` evicts the least
  recently used entry; the next ``get`` for it rebuilds.
- TTL: an entry read at/after its ``expires_at`` is dropped and rebuilt;
  before that it is served from cache.
- Natural turnover (TTL expiry / LRU eviction) does NOT fan out
  invalidation hooks — hooks broadcast config changes only. Explicit
  ``invalidate_*`` keeps fanning out (T2 semantics).
- The ``expert_work_built_agent_cache_entries{scope}`` gauge tracks live
  entry counts for both scopes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from prometheus_client import REGISTRY

from control_plane.runtime import AgentRuntime
from control_plane.subagent_runtime import make_child_agent_builder
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, ToolEnv

_SHA = "a" * 64

_MINIMAL_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "x", "version": "1", "tenant": "test-tenant"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        "system_prompt": {"template": "you help"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    },
}


def _make_spec(*, name: str = "x", version: str = "1") -> AgentSpec:
    manifest = dict(_MINIMAL_MANIFEST)
    manifest["metadata"] = dict(manifest["metadata"], name=name, version=version)
    return AgentSpec.model_validate(manifest)


class _FakeClock:
    """Settable monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _gauge(scope: str) -> float | None:
    return REGISTRY.get_sample_value("expert_work_built_agent_cache_entries", {"scope": scope})


def _make_runtime(
    builds: list[str],
    *,
    cache_max_size: int = 256,
    cache_ttl_s: float = 1800.0,
    clock: _FakeClock | None = None,
) -> AgentRuntime:
    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        builds.append(spec.metadata.name)
        return object()  # stand-in BuiltAgent

    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["_clock"] = clock
    return AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
        cache_max_size=cache_max_size,
        cache_ttl_s=cache_ttl_s,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AgentRuntime._cache — LRU capacity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_cache_capacity_evicts_oldest() -> None:
    """Capacity 2, insert 3 → the oldest entry is evicted and rebuilds on
    the next get; the fresher entries stay cached."""
    builds: list[str] = []
    runtime = _make_runtime(builds, cache_max_size=2)
    t = uuid4()

    for name in ("a", "b", "c"):
        await runtime.get_agent(tenant_id=t, name=name, version="1", spec=_make_spec(name=name))
    assert builds == ["a", "b", "c"]

    # "a" was evicted when "c" arrived → rebuild.
    await runtime.get_agent(tenant_id=t, name="a", version="1", spec=_make_spec(name="a"))
    assert builds == ["a", "b", "c", "a"]

    # "c" survived (it evicted "b" is wrong — "a"'s re-insert evicted "b");
    # "c" is still cached → no rebuild.
    await runtime.get_agent(tenant_id=t, name="c", version="1", spec=_make_spec(name="c"))
    assert builds == ["a", "b", "c", "a"]


@pytest.mark.asyncio
async def test_runtime_cache_get_refreshes_lru_order() -> None:
    """A cache hit moves the entry to most-recently-used, so it survives
    the next eviction."""
    builds: list[str] = []
    runtime = _make_runtime(builds, cache_max_size=2)
    t = uuid4()

    await runtime.get_agent(tenant_id=t, name="a", version="1", spec=_make_spec(name="a"))
    await runtime.get_agent(tenant_id=t, name="b", version="1", spec=_make_spec(name="b"))
    # Touch "a" → "b" becomes LRU.
    await runtime.get_agent(tenant_id=t, name="a", version="1", spec=_make_spec(name="a"))
    # Insert "c" → evicts "b", keeps "a".
    await runtime.get_agent(tenant_id=t, name="c", version="1", spec=_make_spec(name="c"))
    await runtime.get_agent(tenant_id=t, name="a", version="1", spec=_make_spec(name="a"))
    assert builds == ["a", "b", "c"]  # "a" never rebuilt


# ---------------------------------------------------------------------------
# AgentRuntime._cache — TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_cache_ttl_expiry_rebuilds() -> None:
    builds: list[str] = []
    clock = _FakeClock()
    runtime = _make_runtime(builds, cache_ttl_s=100.0, clock=clock)
    t = uuid4()
    spec = _make_spec()

    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec)
    assert builds == ["x"]

    # Not expired yet → served from cache.
    clock.now = 99.0
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec)
    assert builds == ["x"]

    # At expires_at → expired → rebuild.
    clock.now = 100.0
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec)
    assert builds == ["x", "x"]


# ---------------------------------------------------------------------------
# Natural turnover must NOT fan out invalidation hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eviction_and_expiry_do_not_fan_out_hooks() -> None:
    builds: list[str] = []
    clock = _FakeClock()
    runtime = _make_runtime(builds, cache_max_size=1, cache_ttl_s=100.0, clock=clock)
    tenant_hook_calls: list[UUID] = []
    all_hook_calls: list[bool] = []
    user_hook_calls: list[tuple[UUID, str]] = []
    runtime.register_invalidation_hook(tenant_hook_calls.append)

    def _all_hook() -> None:
        all_hook_calls.append(True)

    def _user_hook(tenant_id: UUID, user_id: str) -> None:
        user_hook_calls.append((tenant_id, user_id))

    runtime.register_invalidation_all(_all_hook)
    runtime.register_user_invalidation_hook(_user_hook)
    t = uuid4()

    # LRU eviction (capacity 1, insert 2).
    await runtime.get_agent(tenant_id=t, name="a", version="1", spec=_make_spec(name="a"))
    await runtime.get_agent(tenant_id=t, name="b", version="1", spec=_make_spec(name="b"))
    # TTL expiry.
    clock.now = 100.0
    await runtime.get_agent(tenant_id=t, name="b", version="1", spec=_make_spec(name="b"))
    assert builds == ["a", "b", "b"]

    assert tenant_hook_calls == []
    assert all_hook_calls == []
    assert user_hook_calls == []

    # Explicit invalidation still fans out (T2 semantics unchanged).
    runtime.invalidate_tenant(t)
    assert tenant_hook_calls == [t]


# ---------------------------------------------------------------------------
# Gauge — expert_work_built_agent_cache_entries{scope="runtime"}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_cache_gauge_tracks_put_evict_invalidate() -> None:
    builds: list[str] = []
    runtime = _make_runtime(builds, cache_max_size=2)
    t = uuid4()

    await runtime.get_agent(tenant_id=t, name="a", version="1", spec=_make_spec(name="a"))
    assert _gauge("runtime") == 1.0
    await runtime.get_agent(tenant_id=t, name="b", version="1", spec=_make_spec(name="b"))
    assert _gauge("runtime") == 2.0
    # Eviction keeps the gauge at capacity.
    await runtime.get_agent(tenant_id=t, name="c", version="1", spec=_make_spec(name="c"))
    assert _gauge("runtime") == 2.0

    runtime.invalidate_tenant(t)
    assert _gauge("runtime") == 0.0

    await runtime.get_agent(tenant_id=t, name="a", version="1", spec=_make_spec(name="a"))
    assert _gauge("runtime") == 1.0
    runtime.invalidate_all()
    assert _gauge("runtime") == 0.0


# ---------------------------------------------------------------------------
# Sub-agent closure cache — same bounds
# ---------------------------------------------------------------------------


def _child_spec(name: str, version: str = "1.0.0") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": version, "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "anthropic", "name": "claude"},
                "system_prompt": {"template": "x"},
                "sandbox": {
                    "resources": {"cpu": "1", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": ["a.com"]},
                    "filesystem": {},
                },
            },
        }
    )


@pytest.fixture
def build_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``build_agent`` with a recorder (mirrors test_subagent_runtime)."""
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    return calls


@pytest.mark.asyncio
async def test_subagent_cache_capacity_evicts_oldest(build_calls: list[dict[str, Any]]) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    for name in ("a", "b", "c"):
        await store.create(
            tenant_id=tenant, spec=_child_spec(name), spec_sha256=_SHA, created_by="test"
        )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        cache_max_size=2,
    )

    for name in ("a", "b", "c"):
        await builder(tenant_id=tenant, name=name, version="1.0.0", depth=1)
    assert len(build_calls) == 3
    assert _gauge("subagent") == 2.0

    # "a" was evicted → rebuild; "c" still cached.
    await builder(tenant_id=tenant, name="a", version="1.0.0", depth=1)
    assert len(build_calls) == 4
    await builder(tenant_id=tenant, name="c", version="1.0.0", depth=1)
    assert len(build_calls) == 4


@pytest.mark.asyncio
async def test_subagent_cache_ttl_expiry_rebuilds(build_calls: list[dict[str, Any]]) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_child_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    clock = _FakeClock()
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        cache_ttl_s=100.0,
        clock=clock,
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    clock.now = 99.0
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 1  # not expired → cache hit

    clock.now = 100.0
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 2  # expired → rebuilt
