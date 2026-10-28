"""B-175 P4 — the knowledge catalog survives the run-time Jinja render.

A Jinja agent's ``prompt_base`` (the author's template) is rendered per run and
``prompt_suffix`` is appended unrendered. Base descriptions are tenant text that
may contain template syntax; were the catalog in the rendered part, ``{{ x }}``
would vanish and an unclosed ``{% if %}`` would fail every run of the agent.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from control_plane.prompt_render import render_system_prompt
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import ToolEnv, build_agent
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever

_KEY_NAME = "expert-work/dev/llm/anthropic"


def _jinja_spec() -> AgentSpec:
    spec: dict[str, Any] = {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {
            "template": "You serve {{ client }}.",
            "jinja": True,
            "variables": [{"name": "client"}],
        },
        "knowledge": {"knowledge_base_refs": ["warranty", "specs"]},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    }
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "kb-jinja", "version": "1.0.0", "tenant": "t"},
            "spec": spec,
        }
    )


async def _key_resolver(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY_NAME}"]


@pytest.mark.asyncio
async def test_template_syntax_in_a_description_is_not_rendered() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await store.create_base(
        tenant_id=tenant, name="warranty", description="Covers {{ x }} and {% if y %}"
    )
    await store.create_base(tenant_id=tenant, name="specs")
    async with make_checkpointer("memory") as checkpointer:
        built = await build_agent(
            _jinja_spec(),
            secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
            checkpointer=checkpointer,
            tool_env=ToolEnv(
                knowledge_retriever=KnowledgeRetriever(store=store, embedder=FakeEmbedder())
            ),
            tenant_id=tenant,
            provider_key_resolver=_key_resolver,
        )

    prompt = render_system_prompt(built, {"client": "the support desk"})

    assert prompt.startswith("You serve the support desk.")
    assert 'description="Covers {{ x }} and {% if y %}"' in prompt
    assert prompt.rstrip().endswith("</knowledge_catalog>")
