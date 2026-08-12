"""files[] —— 统一图片 / 文档引用(P2 块 1, Task 10: 只做 image 分发)。

Fixture shape mirrors ``test_external_run_inputs.py`` (Task 9): app + service-
account API-key client scoped to one tenant, plus a local ``AgentRuntime``
builder — ``tests.agent_fixtures.stub_agent_runtime`` discards the manifest by
design, so it can never produce a ``BuiltAgent`` with ``supports_vision=True``
(needed here to pass ``_validate_image_refs``'s vision-capability gate). The
builder below instead forwards ``spec.spec.model.supports_vision`` onto the
built agent, the same way ``test_external_run_inputs.py``'s builder forwards
the jinja fields.

The thread-binding fixtures (``uploaded_image`` / ``other_thread_image``)
bind a real session first (``POST .../sessions``) so the embedded
``thread_id`` in the ``expert_work://image/...`` ref matches (or, for the
"foreign" fixture, deliberately does NOT match) the thread the run call
actually addresses — a ref built with a random thread_id could never satisfy
the cross-thread check ``_validate_image_refs`` enforces, so no run could
ever legitimately accept it, and a "success" test that never triggers that
check does not test anything.
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
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore, RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import BuiltAgent, GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "vision-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-5",
            "supports_vision": True,
        },
        "system_prompt": {"template": "you are a helpful assistant"},
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


def _vision_aware_agent_runtime(
    *, run_store: InMemoryRunStore, run_event_store: InMemoryRunEventStore
) -> AgentRuntime:
    """Same stub shape as ``test_external_run_inputs.py``'s builder, but
    forwards ``spec.spec.model.supports_vision`` so ``_validate_image_refs``'s
    vision-capability gate (Path A) can actually pass in these tests."""

    async def _build(
        spec: AgentSpec, *, tenant_id: object | None = None, user_id: str | None = None
    ) -> BuiltAgent:
        del tenant_id, user_id
        graph = GraphRunner(checkpointer=InMemorySaver()).compile(
            build_react_graph(llm_caller=_fake_llm, tool_registry=ToolRegistry())
        )
        return BuiltAgent(
            graph=graph,
            system_prompt=spec.spec.system_prompt.template,
            max_steps=5,
            supports_vision=spec.spec.model.supports_vision,
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
        agent_runtime=_vision_aware_agent_runtime(
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
class _VisionAgent:
    code: str


@pytest.fixture
async def vision_agent(_external_ctx: _ExternalCtx) -> _VisionAgent:
    await _external_ctx.app.state.agent_spec_repo.create(
        tenant_id=_external_ctx.tenant_id,
        spec=_spec(),
        spec_sha256="d" * 64,
        created_by="seed",
    )
    return _VisionAgent(code="vision-bot")


@dataclass
class _BoundImage:
    """An ``expert_work://image/...`` ref bound to a real, resolvable session."""

    uri: str
    session_id: UUID


async def _bind_image(
    *, client: AsyncClient, agent_code: str, tenant_id: UUID, user_id: str
) -> _BoundImage:
    resp = await client.post(f"/v1/agents/{agent_code}/sessions", json={"user_id": user_id})
    assert resp.status_code == 201, resp.text
    session_id = UUID(resp.json()["data"]["session_id"])
    ref = ImageRef(tenant_id=tenant_id, thread_id=session_id, image_id=uuid4(), ext=".png")
    return _BoundImage(uri=ref.to_uri(), session_id=session_id)


@pytest.fixture
async def uploaded_image(
    external_client: AsyncClient, vision_agent: _VisionAgent, _external_ctx: _ExternalCtx
) -> _BoundImage:
    return await _bind_image(
        client=external_client,
        agent_code=vision_agent.code,
        tenant_id=_external_ctx.tenant_id,
        user_id="u1",
    )


@pytest.fixture
async def other_thread_image(
    external_client: AsyncClient, vision_agent: _VisionAgent, _external_ctx: _ExternalCtx
) -> _BoundImage:
    """Bound to a *different* user's session than ``uploaded_image`` — the
    reject test addresses ``uploaded_image``'s thread but supplies a file
    ref from this one, exercising the cross-thread branch of
    ``_validate_image_refs``."""
    return await _bind_image(
        client=external_client,
        agent_code=vision_agent.code,
        tenant_id=_external_ctx.tenant_id,
        user_id="u2",
    )


@pytest.mark.asyncio
async def test_image_file_ref_merges_into_image_refs(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    uploaded_image: _BoundImage,
    _external_ctx: _ExternalCtx,
) -> None:
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(uploaded_image.session_id),
            "input": "看图",
            "mode": "queue",
            "files": [
                {"type": "image", "transfer_method": "local_file", "upload_id": uploaded_image.uri}
            ],
        },
    )
    assert resp.status_code == 202, resp.text

    # 202 alone doesn't prove the file ref actually merged into image_refs —
    # queue mode only persists ``enqueued_input`` synchronously (the graph
    # never runs in this test). Read the persisted run back and assert the
    # merged list landed exactly as expected.
    run_id = UUID(resp.json()["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["image_refs"] == [uploaded_image.uri]


@pytest.mark.asyncio
async def test_image_file_ref_merges_alongside_existing_image_refs(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    uploaded_image: _BoundImage,
    _external_ctx: _ExternalCtx,
) -> None:
    """files[] and the pre-existing image_refs field co-exist — both must
    land in the merged list when a single request supplies both."""
    legacy_ref = ImageRef(
        tenant_id=_external_ctx.tenant_id,
        thread_id=uploaded_image.session_id,
        image_id=uuid4(),
        ext=".jpg",
    ).to_uri()
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(uploaded_image.session_id),
            "input": "看图",
            "mode": "queue",
            "image_refs": [legacy_ref],
            "files": [
                {"type": "image", "transfer_method": "local_file", "upload_id": uploaded_image.uri}
            ],
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["image_refs"] == [legacy_ref, uploaded_image.uri]


@pytest.mark.asyncio
async def test_unknown_transfer_method_is_422(
    external_client: AsyncClient, vision_agent: _VisionAgent
) -> None:
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "files": [{"type": "image", "transfer_method": "remote_url", "upload_id": "http://x"}],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_document_type_is_accepted_by_the_model(
    external_client: AsyncClient, vision_agent: _VisionAgent, uploaded_image: _BoundImage
) -> None:
    """``type: "document"`` must not be a 422 at the model layer — the
    enum is meant to be live now even though the dispatch (folding it
    somewhere useful) is Task 11's job. This only proves the request body
    is accepted; it says nothing about what happens to the document ref."""
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(uploaded_image.session_id),
            "input": "x",
            "mode": "queue",
            "files": [{"type": "document", "transfer_method": "local_file", "upload_id": "doc-1"}],
        },
    )
    assert resp.status_code != 422


@pytest.mark.asyncio
async def test_foreign_thread_image_ref_rejected(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    uploaded_image: _BoundImage,
    other_thread_image: _BoundImage,
) -> None:
    """A files[] image ref bound to a *different* session's thread must be
    rejected by the same cross-thread check ``_validate_image_refs`` already
    enforces for plain ``image_refs`` — the run addresses ``uploaded_image``'s
    session but the file ref belongs to ``other_thread_image``'s session.
    ``_validate_image_refs`` raises this branch as 404 (cross-tenant / cross-
    thread refs are made to look identical to "not found" — runs.py:236-237),
    not 400/422."""
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(uploaded_image.session_id),
            "input": "x",
            "mode": "queue",
            "files": [
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_id": other_thread_image.uri,
                }
            ],
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_files_array_over_64_is_422(
    external_client: AsyncClient, vision_agent: _VisionAgent
) -> None:
    files = [
        {"type": "document", "transfer_method": "local_file", "upload_id": f"doc-{i}"}
        for i in range(65)
    ]
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={"user_id": "u1", "input": "x", "mode": "queue", "files": files},
    )
    assert resp.status_code == 422
