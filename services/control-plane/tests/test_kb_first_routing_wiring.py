"""B-267 — the platform switch ``kb_first_routing`` and the ``web_search``
fixture directory, from settings to the build path.

The switch is read once at boot (env ``EXPERT_WORK_KB_FIRST_ROUTING``); a change
means a restart, which empties the in-process build caches. What the caches do
key on is the catalog the switch decides — with it on, one bound base has a
catalog too, so the key's catalog slot must read it the way the build does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.app import create_app
from control_plane.runtime import AgentRuntime, build_tool_env, knowledge_catalog_fingerprint
from control_plane.settings import Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import BuiltAgent, ToolEnv
from orchestrator.tools.knowledge_catalog import catalog_fingerprint
from tests.auth_fixtures import build_test_jwt_verifier

_MANIFEST: dict[str, Any] = {
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
        "knowledge": {"knowledge_base_refs": ["docs"]},
    },
}


class _TenantConfig:
    async def get(self, *, tenant_id: UUID, actor_id: str | None = None) -> object:
        return SimpleNamespace(http_tool_allowlist=[], http_tool_denylist=[])


# --------------------------------------------------------------------------- settings


def test_the_switch_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXPERT_WORK_KB_FIRST_ROUTING", raising=False)
    monkeypatch.delenv("EXPERT_WORK_WEB_SEARCH_FIXTURES_DIR", raising=False)
    settings = Settings()
    assert settings.kb_first_routing is True
    assert settings.web_search_fixtures_dir is None


def test_both_settings_read_their_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_KB_FIRST_ROUTING", "false")
    monkeypatch.setenv("EXPERT_WORK_WEB_SEARCH_FIXTURES_DIR", "/etc/expert-work/fixtures")
    settings = Settings()
    assert settings.kb_first_routing is False
    assert settings.web_search_fixtures_dir == "/etc/expert-work/fixtures"


def test_build_tool_env_carries_both() -> None:
    env = build_tool_env(
        _TenantConfig(),  # type: ignore[arg-type]
        kb_first_routing=True,
        web_search_fixtures_dir="/fixtures",
    )
    assert env.kb_first_routing is True
    assert env.web_search_fixtures_dir == "/fixtures"
    unwired = build_tool_env(_TenantConfig())  # type: ignore[arg-type]
    assert unwired.kb_first_routing is False
    assert unwired.web_search_fixtures_dir is None


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [False, True], ids=["off", "on"])
async def test_lifespan_passes_the_settings_to_the_build_path(
    monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    seen: list[ToolEnv] = []

    def _spy(*args: Any, **kwargs: Any) -> ToolEnv:
        env = build_tool_env(*args, **kwargs)
        seen.append(env)
        return env

    monkeypatch.setattr("control_plane.app.build_tool_env", _spy)
    app = create_app(
        settings=Settings(
            checkpointer_backend="memory",
            kb_first_routing=switch,
            web_search_fixtures_dir="/fixtures" if switch else None,
        ),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    async with app.router.lifespan_context(app):
        assert app.state.agent_runtime.kb_first_routing is switch
    [env] = seen
    assert env.kb_first_routing is switch
    assert env.web_search_fixtures_dir == ("/fixtures" if switch else None)


# --------------------------------------------------------------------------- cache key


async def _store_with(tenant: UUID, name: str = "docs") -> tuple[InMemoryKnowledgeStore, UUID]:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=tenant, name=name, description="about docs")
    return store, base.id


@pytest.mark.asyncio
async def test_the_key_slot_reads_a_single_base_only_with_the_switch() -> None:
    tenant = uuid4()
    store, _ = await _store_with(tenant)
    spec = AgentSpec.model_validate(_MANIFEST)
    assert await knowledge_catalog_fingerprint(store, spec, tenant_id=tenant) is None
    keyed = await knowledge_catalog_fingerprint(store, spec, tenant_id=tenant, single_base=True)
    assert keyed is not None
    assert keyed == await catalog_fingerprint(
        store, tenant_id=tenant, refs=("docs",), single_base=True
    )


@pytest.mark.asyncio
async def test_a_single_base_build_is_cached_and_follows_catalog_edits() -> None:
    """With the switch on the build freezes a lone base's catalog; the runtime's
    key must carry the same fingerprint, or no such build would ever be cached —
    and an edited description must reach the next run."""
    tenant = uuid4()
    store, kb_id = await _store_with(tenant)
    builds: list[str | None] = []

    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> BuiltAgent:
        assert tenant_id is not None
        fingerprint = await catalog_fingerprint(
            store, tenant_id=tenant_id, refs=("docs",), single_base=True
        )
        builds.append(fingerprint)
        return BuiltAgent(
            graph=object(),  # type: ignore[arg-type]
            system_prompt="",
            max_steps=1,
            knowledge_catalog_fingerprint=fingerprint,
        )

    runtime = AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,
        knowledge_store=store,
        kb_first_routing=True,
    )
    spec = AgentSpec.model_validate(_MANIFEST)
    await runtime.get_agent(tenant_id=tenant, name="x", version="1", spec=spec)
    await runtime.get_agent(tenant_id=tenant, name="x", version="1", spec=spec)
    assert len(builds) == 1
    await store.update_base(tenant_id=tenant, kb_id=kb_id, description="edited")
    await runtime.get_agent(tenant_id=tenant, name="x", version="1", spec=spec)
    assert len(builds) == 2
    assert builds[0] != builds[1]
