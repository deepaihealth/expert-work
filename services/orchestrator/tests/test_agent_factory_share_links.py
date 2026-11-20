"""B-326 —— ``build_agent`` 按 ``spec.share_links`` 接线:开了 → ``share_file`` 进工具表、
``share_links`` 交给图(终局回复里换占位符);没开 → 两样都没有,图拿到 ``None``。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from expert_work.persistence import InMemoryArtifactStore
from expert_work.persistence.artifact_share import InMemoryArtifactShareLinkStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import build_agent, build_react_graph
from orchestrator.llm.endpoints import ProviderKey
from orchestrator.tools import ToolEnv
from orchestrator.tools.share_file import SHARE_FILE_TOOL_NAME, ShareLinkService

_REF = "secret://expert-work/dev/llm/anthropic"
_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "share-agent", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {"template": "you are a test agent"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


async def _resolver(provider: str) -> list[ProviderKey]:
    del provider
    return [ProviderKey(_REF)]


async def _build(
    monkeypatch: pytest.MonkeyPatch, share_links: dict[str, Any] | None
) -> tuple[Any, list[Any], ShareLinkService]:
    doc = deepcopy(_SPEC)
    if share_links is not None:
        doc["spec"]["share_links"] = share_links
    service = ShareLinkService(
        links=InMemoryArtifactShareLinkStore(),
        artifacts=InMemoryArtifactStore(),
        public_base_url="https://files.example.test",
    )
    seen: list[Any] = []

    def _spy(**kwargs: Any) -> Any:
        seen.append(kwargs.get("share_links_ttl_minutes"))
        return build_react_graph(**kwargs)

    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            AgentSpec.model_validate(doc),
            secret_store=LocalDevSecretStore.from_mapping({"expert-work/dev/llm/anthropic": "k"}),
            checkpointer=cp,
            provider_key_resolver=_resolver,
            tool_env=ToolEnv(share_links=service),
        )
    return built, seen, service


@pytest.mark.asyncio
async def test_opted_in_agent_gets_tool_and_its_lifetime_on_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built, seen, _ = await _build(monkeypatch, {"enabled": True, "ttl_minutes": 60})
    assert SHARE_FILE_TOOL_NAME in {entry.name for entry in built.tool_catalog}
    assert seen == [60]


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [None, {"enabled": False}])
async def test_agent_without_opt_in_gets_neither(
    monkeypatch: pytest.MonkeyPatch, block: dict[str, Any] | None
) -> None:
    built, seen, _ = await _build(monkeypatch, block)
    assert SHARE_FILE_TOOL_NAME not in {entry.name for entry in built.tool_catalog}
    assert seen == [None]
