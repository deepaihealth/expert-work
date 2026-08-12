"""对外 run 端点透传 inputs(模板变量)。校验逻辑复用内部现成的。

External-API-v1 P2 Task 9. Fixture shape mirrors ``test_external_sessions.py``
/ ``test_external_api_contract.py`` (app + service-account API-key client
scoped to one tenant), plus a jinja-aware ``AgentRuntime`` builder this task
needs and no existing test fixture provides: ``tests.agent_fixtures.
stub_agent_runtime`` discards the manifest by design (its own docstring says
so), so every ``BuiltAgent`` it returns has ``prompt_jinja=False`` regardless
of what the seeded agent declares — that would make ``validate_prompt_inputs``
(called inside ``spawn_run``) reject ANY non-empty ``inputs`` with "agent
declares no prompt variables", never reaching the 64-key / unknown-key /
missing-required checks this task's tests exercise. ``_jinja_aware_agent_
runtime`` below is the same stub shape but forwards ``spec.spec.system_
prompt.jinja`` / ``.variables`` onto the built agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore, RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import BuiltAgent, GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "jinja-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {
            "template": "you are a {{ lang }} speaking support agent",
            "jinja": True,
            "variables": [{"name": "lang", "required": False}],
        },
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


async def _fake_llm(
    *,
    messages: Sequence[BaseMessage],
    tools: Sequence[ToolSpec],
    on_delta: Callable[[Any], Awaitable[None]] | None = None,
) -> AIMessage:
    del messages, tools, on_delta
    return AIMessage(content="stub agent reply", id="ai-stub")


def _jinja_aware_agent_runtime(
    *, run_store: InMemoryRunStore, run_event_store: InMemoryRunEventStore
) -> AgentRuntime:
    async def _build(
        spec: AgentSpec, *, tenant_id: object | None = None, user_id: str | None = None
    ) -> BuiltAgent:
        del tenant_id, user_id
        graph = GraphRunner(checkpointer=InMemorySaver()).compile(
            build_react_graph(llm_caller=_fake_llm, tool_registry=ToolRegistry())
        )
        sp = spec.spec.system_prompt
        return BuiltAgent(
            graph=graph,
            system_prompt=sp.template,
            max_steps=5,
            prompt_jinja=sp.jinja,
            prompt_variables=tuple(sp.variables),
        )

    return AgentRuntime(
        run_manager=RunManager(store=run_store),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_build,
        run_event_store=run_event_store,
    )


@dataclass
class _ExternalCtx:
    app: Any
    tenant_id: UUID
    client: AsyncClient
    run_store: InMemoryRunStore


@pytest.fixture
async def _external_ctx() -> AsyncIterator[_ExternalCtx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=_jinja_aware_agent_runtime(
            run_store=run_store, run_event_store=run_event_store
        ),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    tenant_id = uuid4()
    # A real third-party caller is a service-account (API-key) principal —
    # matches ``test_external_api_contract.py`` / ``test_external_sessions.py``.
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-external-app",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {jwt}"},
    ) as client:
        yield _ExternalCtx(app=app, tenant_id=tenant_id, client=client, run_store=run_store)


@pytest.fixture
def external_client(_external_ctx: _ExternalCtx) -> AsyncClient:
    return _external_ctx.client


@dataclass
class _JinjaAgent:
    code: str


@pytest.fixture
async def jinja_agent(_external_ctx: _ExternalCtx) -> _JinjaAgent:
    await _external_ctx.app.state.agent_spec_repo.create(
        tenant_id=_external_ctx.tenant_id,
        spec=_spec(),
        spec_sha256="b" * 64,
        created_by="seed",
    )
    return _JinjaAgent(code="jinja-bot")


@pytest.mark.asyncio
async def test_inputs_reaches_prompt_render(
    external_client: AsyncClient, jinja_agent: _JinjaAgent, _external_ctx: _ExternalCtx
) -> None:
    resp = await external_client.post(
        f"/v1/agents/{jinja_agent.code}/runs",
        json={"user_id": "u1", "input": "hi", "mode": "queue", "inputs": {"lang": "zh"}},
    )
    assert resp.status_code == 202

    # 202 alone doesn't prove ``inputs`` reached anything — queue mode
    # (``runs.py:830-850``) only persists ``enqueued_input`` synchronously;
    # ``render_system_prompt`` runs later, in the queue worker, out of this
    # request/response cycle. Read the persisted run back and assert the
    # external ``inputs`` actually landed in ``enqueued_input`` — the exact
    # dict the worker will hand to ``build_run_graph_input`` /
    # ``render_system_prompt`` when it claims this run.
    run_id = UUID(resp.json()["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["inputs"] == {"lang": "zh"}


@pytest.mark.asyncio
async def test_undeclared_input_key_is_422(external_client, jinja_agent) -> None:
    resp = await external_client.post(
        f"/v1/agents/{jinja_agent.code}/runs",
        json={"user_id": "u1", "input": "hi", "mode": "queue", "inputs": {"没声明的键": "x"}},
    )
    assert resp.status_code == 422
