"""files[] —— 对外附件模型统一(spec 2026-08-17,Task 3): ``files[]`` 每条只带
一个 ``upload_id``,按 ``UserUploadStore`` 查表分流成内部 ``image_refs`` /
``document_names``。``image_refs`` 顶层字段与 ``files[].type`` /
``transfer_method`` 均已删除(``extra="forbid"`` 拒绝任何残留)。

Fixture shape mirrors ``test_external_run_inputs.py`` (Task 9): app + service-
account API-key client scoped to one tenant, plus a local ``AgentRuntime``
builder — ``tests.agent_fixtures.stub_agent_runtime`` discards the manifest by
design, so it can never produce a ``BuiltAgent`` with ``supports_vision=True``
(needed here to pass ``_validate_image_refs``'s vision-capability gate). The
builder below instead forwards ``spec.spec.model.supports_vision`` onto the
built agent, the same way ``test_external_run_inputs.py``'s builder forwards
the jinja fields.

Every ``files[]`` entry now addresses a row in ``user_upload`` — seeded
directly via ``app.state.user_upload_store.insert(...)`` (in-memory), per the
brief: a real ``tenant_user`` must exist FIRST (bound through a real
``POST .../sessions`` call) before a seeded row's ``user_id`` means anything,
otherwise the run request short-circuits at ``end_user_id is None`` and never
reaches the branch under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

import control_plane.api.agents as agents_module
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.protocol import AgentSpec, UserUpload, render_upload_id
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


# Task 11 — a non-vision agent, to prove document dispatch does not depend on
# ``supports_vision`` (only the image-ref path is gated on it).
_PLAIN_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "plain-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-5",
            "supports_vision": False,
        },
        "system_prompt": {"template": "you are a helpful assistant"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _plain_spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_PLAIN_SPEC))


@dataclass
class _PlainAgent:
    code: str


@pytest.fixture
async def plain_agent(_external_ctx: _ExternalCtx) -> _PlainAgent:
    await _external_ctx.app.state.agent_spec_repo.create(
        tenant_id=_external_ctx.tenant_id,
        spec=_plain_spec(),
        spec_sha256="e" * 64,
        created_by="seed",
    )
    return _PlainAgent(code="plain-bot")


@dataclass
class _BoundSession:
    """A real ``tenant_user`` + thread, minted through ``POST .../sessions`` —
    seeding a ``user_upload`` row requires a real ``(tenant_user.id,
    thread_id)`` pair for it to plausibly belong to; a request under this
    ``user_id`` must reach ``end_user_id`` == this ``user_id`` (this repo has
    hit the "request short-circuits at ``end_user_id is None``" trap four
    times — seed the tenant_user FIRST)."""

    session_id: UUID
    user_id: UUID


async def _bind_session(*, client: AsyncClient, agent_code: str, user_id: str) -> _BoundSession:
    resp = await client.post(f"/v1/agents/{agent_code}/sessions", json={"user_id": user_id})
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    return _BoundSession(session_id=UUID(data["session_id"]), user_id=UUID(data["user_id"]))


@pytest.fixture
async def session_u1(external_client: AsyncClient, vision_agent: _VisionAgent) -> _BoundSession:
    return await _bind_session(client=external_client, agent_code=vision_agent.code, user_id="u1")


@pytest.fixture
async def session_u2(external_client: AsyncClient, vision_agent: _VisionAgent) -> _BoundSession:
    """A *different* end-user's own session — used for the cross-user and
    cross-thread rejection tests."""
    return await _bind_session(client=external_client, agent_code=vision_agent.code, user_id="u2")


async def _seed_image(
    *, store: UserUploadStore, tenant_id: UUID, owner: _BoundSession
) -> UserUpload:
    ref = ImageRef(
        tenant_id=tenant_id, thread_id=owner.session_id, image_id=uuid4(), ext=".png"
    ).to_uri()
    return await store.insert(
        upload_id=uuid4(),
        tenant_id=tenant_id,
        user_id=owner.user_id,
        thread_id=owner.session_id,
        kind="image",
        ref=ref,
        mime_type="image/png",
        size_bytes=100,
        filename="photo.png",
    )


async def _seed_document(
    *,
    store: UserUploadStore,
    tenant_id: UUID,
    owner: _BoundSession,
    ref: str = "uploads/report.pdf",
) -> UserUpload:
    return await store.insert(
        upload_id=uuid4(),
        tenant_id=tenant_id,
        user_id=owner.user_id,
        thread_id=owner.session_id,
        kind="document",
        ref=ref,
        mime_type="application/pdf",
        size_bytes=200,
        filename="report.pdf",
    )


@pytest.fixture
async def uploaded_image(_external_ctx: _ExternalCtx, session_u1: _BoundSession) -> UserUpload:
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    return await _seed_image(store=store, tenant_id=_external_ctx.tenant_id, owner=session_u1)


# ---------------------------------------------------------------------------
# extra="forbid" — the removed fields (top-level image_refs, files[].type /
# .transfer_method) must 422, not be silently ignored.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_level_image_refs_field_is_422(
    external_client: AsyncClient, vision_agent: _VisionAgent
) -> None:
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "image_refs": ["expert_work://image/x/x/x"],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_files_item_with_legacy_type_field_is_422(
    external_client: AsyncClient, vision_agent: _VisionAgent
) -> None:
    """The old ``files[]`` shape (``type`` / ``transfer_method`` alongside
    ``upload_id``) is rejected outright — ``ExternalFileRef`` now only knows
    ``upload_id``."""
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": "upl_" + "0" * 36, "type": "image"}],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# upload_id resolution — malformed shape, unknown row, wrong owner, wrong
# thread.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_upload_id_shape_is_422(
    external_client: AsyncClient, vision_agent: _VisionAgent
) -> None:
    """A pre-unification document id (``uploads/<name>``) is a well-formed
    string (passes ``ExternalFileRef.upload_id``'s own length bounds) but is
    not a ``upl_<uuid>`` — ``parse_upload_id`` returns ``None``."""
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": "uploads/report.pdf"}],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_UPLOAD_ID"


@pytest.mark.asyncio
async def test_unknown_upload_id_is_404(
    external_client: AsyncClient, vision_agent: _VisionAgent
) -> None:
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": render_upload_id(uuid4())}],
        },
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "UPLOAD_NOT_FOUND"


@pytest.mark.asyncio
async def test_upload_owned_by_other_user_is_404(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    _external_ctx: _ExternalCtx,
    session_u1: _BoundSession,
    session_u2: _BoundSession,
) -> None:
    """A row that genuinely exists but belongs to a different end-user is
    made to look identical to "unknown" — the endpoint must not reveal that
    the id exists at all.

    ``thread_id`` is deliberately seeded to match the session THIS run
    addresses (``session_u1``) while ``user_id`` belongs to ``session_u2`` —
    isolating the ownership check from the thread-binding check. A row that
    also had the wrong thread would 404 via that other check even with the
    ownership check deleted, silently certifying a broken ownership check as
    passing (confirmed by mutation self-check — see Task 3 report)."""
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    other_users_image = await store.insert(
        upload_id=uuid4(),
        tenant_id=_external_ctx.tenant_id,
        user_id=session_u2.user_id,
        thread_id=session_u1.session_id,
        kind="image",
        ref=ImageRef(
            tenant_id=_external_ctx.tenant_id,
            thread_id=session_u1.session_id,
            image_id=uuid4(),
            ext=".png",
        ).to_uri(),
        mime_type="image/png",
        size_bytes=100,
        filename="photo.png",
    )
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session_u1.session_id),
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": render_upload_id(other_users_image.id)}],
        },
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "UPLOAD_NOT_FOUND"


@pytest.mark.asyncio
async def test_image_row_bound_to_a_different_thread_is_404(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    _external_ctx: _ExternalCtx,
    session_u1: _BoundSession,
) -> None:
    """Same owner, but the image row is bound to a thread other than the one
    this run addresses — the ADR-0004 cross-thread rule, enforced here (on
    the registry's own ``thread_id``) instead of deep inside
    ``_validate_image_refs``."""
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    other_session = await _bind_session(
        client=external_client, agent_code=vision_agent.code, user_id="u1"
    )
    assert other_session.session_id != session_u1.session_id
    foreign_thread_image = await store.insert(
        upload_id=uuid4(),
        tenant_id=_external_ctx.tenant_id,
        user_id=session_u1.user_id,
        thread_id=other_session.session_id,
        kind="image",
        ref=ImageRef(
            tenant_id=_external_ctx.tenant_id,
            thread_id=other_session.session_id,
            image_id=uuid4(),
            ext=".png",
        ).to_uri(),
        mime_type="image/png",
        size_bytes=100,
        filename="photo.png",
    )
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session_u1.session_id),
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": render_upload_id(foreign_thread_image.id)}],
        },
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "UPLOAD_NOT_FOUND"


# ---------------------------------------------------------------------------
# Successful dispatch — spawn_run receives the resolved refs, not the
# client-supplied upload_id strings.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_image_one_document_dispatch_to_spawn_run(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    _external_ctx: _ExternalCtx,
    session_u1: _BoundSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single ``files[]`` array carrying one image and one document row
    must fan out into both internal channels: ``spawn_run`` receives
    ``payload.image_refs == [row.ref]`` (the registry's storage ref, not the
    opaque ``upload_id``) and ``payload.document_names == [row.ref]``.
    ``spawn_run`` itself is monkeypatched so this asserts exactly what
    reaches it, rather than inferring it indirectly from the persisted run."""
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    image_row = await _seed_image(store=store, tenant_id=_external_ctx.tenant_id, owner=session_u1)
    doc_row = await _seed_document(store=store, tenant_id=_external_ctx.tenant_id, owner=session_u1)

    captured: dict[str, Any] = {}

    async def _fake_spawn_run(**kwargs: Any) -> JSONResponse:
        captured["image_refs"] = kwargs["payload"].image_refs
        captured["document_names"] = kwargs["payload"].document_names
        return JSONResponse(
            status_code=202,
            content={"success": True, "data": {"run_id": str(uuid4())}, "error": None},
        )

    monkeypatch.setattr(agents_module, "spawn_run", _fake_spawn_run)

    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session_u1.session_id),
            "input": "看图并总结附件",
            "mode": "queue",
            "files": [
                {"upload_id": render_upload_id(image_row.id)},
                {"upload_id": render_upload_id(doc_row.id)},
            ],
        },
    )
    assert resp.status_code == 202, resp.text
    assert captured["image_refs"] == [image_row.ref]
    assert captured["document_names"] == [doc_row.ref]


# ---------------------------------------------------------------------------
# Count bounds — the field-level max_length=64, and the internal
# MAX_RUN_IMAGE_REFS defense-in-depth backstop behind it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_array_over_64_is_422(
    external_client: AsyncClient, vision_agent: _VisionAgent
) -> None:
    files = [{"upload_id": render_upload_id(uuid4())} for _ in range(65)]
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={"user_id": "u1", "input": "x", "mode": "queue", "files": files},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_image_count_over_max_run_image_refs_is_422(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    _external_ctx: _ExternalCtx,
    session_u1: _BoundSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``files[]``'s own field-level ``max_length=64`` ties exactly to
    ``MAX_RUN_IMAGE_REFS`` today, so a single request can never carry more
    than ``MAX_RUN_IMAGE_REFS`` resolvable image entries — the internal count
    check is a defense-in-depth backstop for if the two ever drift apart.
    Proven here by lowering ``MAX_RUN_IMAGE_REFS`` (not the field bound) and
    sending one more resolvable image than that lowered limit allows — real,
    resolvable rows, so the count check (not an earlier 404/422) is what
    fires."""
    monkeypatch.setattr(agents_module, "MAX_RUN_IMAGE_REFS", 2)
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    rows = [
        await _seed_image(store=store, tenant_id=_external_ctx.tenant_id, owner=session_u1)
        for _ in range(3)
    ]
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session_u1.session_id),
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": render_upload_id(row.id)} for row in rows],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "TOO_MANY_IMAGE_REFS"
    # The new message talks about files[], not the removed image_refs field.
    assert "files[]" in body["error"]["message"]
    assert "image_refs" not in body["error"]["message"]


# ---------------------------------------------------------------------------
# Vision-capability gate — unchanged behaviour, now reached through the
# upload_id resolution path instead of a raw image_refs string.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_incapable_agent_with_image_is_422(
    external_client: AsyncClient,
    plain_agent: _PlainAgent,
    _external_ctx: _ExternalCtx,
) -> None:
    session = await _bind_session(client=external_client, agent_code=plain_agent.code, user_id="u1")
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    image_row = await _seed_image(store=store, tenant_id=_external_ctx.tenant_id, owner=session)
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session.session_id),
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": render_upload_id(image_row.id)}],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "does not accept image input" in resp.text


# ---------------------------------------------------------------------------
# Document dispatch — path-traversal defense-in-depth on the registry's own
# ``ref`` (the client no longer supplies this string directly — it addresses
# it indirectly through upload_id — but the same gate still guards against a
# malformed ref reaching the workspace).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_ref_path_traversal_rejected(
    external_client: AsyncClient,
    plain_agent: _PlainAgent,
    _external_ctx: _ExternalCtx,
) -> None:
    session = await _bind_session(client=external_client, agent_code=plain_agent.code, user_id="u1")
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    bad_row = await _seed_document(
        store=store,
        tenant_id=_external_ctx.tenant_id,
        owner=session,
        ref="uploads/../../etc/passwd",
    )
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session.session_id),
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": render_upload_id(bad_row.id)}],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_FILE_REF"


@pytest.mark.asyncio
async def test_document_empty_upload_id_rejected(
    external_client: AsyncClient, plain_agent: _PlainAgent
) -> None:
    """The empty string is rejected too — but earlier than upload_id
    resolution: ``ExternalFileRef.upload_id`` itself has ``min_length=1``, so
    this surfaces the app-wide ``/v1/agents/`` validation envelope
    (``INVALID_REQUEST``), not ``INVALID_UPLOAD_ID``."""
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "files": [{"upload_id": ""}],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_document_name_lands_in_prompt() -> None:
    from control_plane.api.runs import _build_human_message

    msg = _build_human_message(
        input_text="总结这份文件",
        image_refs=[],
        supports_vision=False,
        document_names=["合同.pdf"],
    )
    assert "[file attached: 合同.pdf]" in msg.content


@pytest.mark.asyncio
async def test_document_ref_lands_in_enqueued_payload(
    external_client: AsyncClient, plain_agent: _PlainAgent, _external_ctx: _ExternalCtx
) -> None:
    """A 202 alone doesn't prove the document name actually reached
    ``RunRequest.document_names`` — read the persisted ``enqueued_input``
    back (queue mode only persists it synchronously; the graph never runs in
    this test) and assert the resolved registry ``ref`` landed exactly."""
    session = await _bind_session(client=external_client, agent_code=plain_agent.code, user_id="u1")
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    doc_row = await _seed_document(store=store, tenant_id=_external_ctx.tenant_id, owner=session)
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session.session_id),
            "input": "总结这份文件",
            "mode": "queue",
            "files": [{"upload_id": render_upload_id(doc_row.id)}],
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["data"]["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["document_names"] == [doc_row.ref]


@pytest.mark.asyncio
async def test_mixed_image_and_document_files_dispatch_to_both_channels(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    _external_ctx: _ExternalCtx,
    session_u1: _BoundSession,
    uploaded_image: UserUpload,
) -> None:
    """A single ``files[]`` array carrying one image and one document must
    fan out into both channels: the image lands in ``image_refs`` (the
    existing multimodal path), the document lands in ``document_names`` —
    independently, in the same request. Reads the persisted run back (unlike
    the spy test above) so this is an end-to-end proof through the real
    ``spawn_run``, not a mock."""
    store: UserUploadStore = _external_ctx.app.state.user_upload_store
    doc_row = await _seed_document(
        store=store, tenant_id=_external_ctx.tenant_id, owner=session_u1, ref="uploads/summary.docx"
    )
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(session_u1.session_id),
            "input": "看图并总结附件",
            "mode": "queue",
            "files": [
                {"upload_id": render_upload_id(uploaded_image.id)},
                {"upload_id": render_upload_id(doc_row.id)},
            ],
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["data"]["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["image_refs"] == [uploaded_image.ref]
    assert run.enqueued_input["document_names"] == ["uploads/summary.docx"]


@pytest.mark.asyncio
async def test_no_files_document_names_stays_empty(
    external_client: AsyncClient, plain_agent: _PlainAgent, _external_ctx: _ExternalCtx
) -> None:
    """``files[]`` omitted entirely — the resolution block must not perturb
    a plain run with no attachments."""
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={"user_id": "u1", "input": "hello", "mode": "queue"},
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["data"]["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["document_names"] == []
    assert run.enqueued_input["image_refs"] == []


# ---------------------------------------------------------------------------
# untrusted_content — unrelated to files[]/image_refs, kept unchanged from
# before this rewrite (P2-a security-review fix, Critical): RunRequest is
# hand-constructed past FastAPI's own request-body validation path, so an
# unguarded oversized block used to be a bare 500, not a 422.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_untrusted_content_block_over_limit_is_422_not_500(
    external_client: AsyncClient, plain_agent: _PlainAgent
) -> None:
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "untrusted_content": ["x" * 9000],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "UNTRUSTED_CONTENT_BLOCK_TOO_LONG"


@pytest.mark.asyncio
async def test_untrusted_content_block_at_exactly_8192_is_not_422(
    external_client: AsyncClient, plain_agent: _PlainAgent
) -> None:
    """Boundary-legal: the bound is a strict ``>``, so a block of exactly
    8192 chars (``MAX_UNTRUSTED_CONTENT_BLOCK_CHARS``) must be accepted."""
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "untrusted_content": ["x" * 8192],
        },
    )
    assert resp.status_code != 422, resp.text
    assert resp.status_code == 202, resp.text
