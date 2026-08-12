"""files[] —— 统一图片 / 文档引用(P2 块 1, Task 10: image 分发;Task 11: 文档
分发 + 路径净化闸)。

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
    """``type: "document"`` must not be a 422 at the model layer.

    修复轮 1:``upload_id`` 改成 ``uploads/doc-1.txt``(Task 11 收紧闸之后,
    真正合法的 upload_id 形状必须带 ``uploads/`` 前缀 —— 裸文件名不再是
    合法值,见 ``is_safe_document_upload_id``)。"""
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(uploaded_image.session_id),
            "input": "x",
            "mode": "queue",
            "files": [
                {
                    "type": "document",
                    "transfer_method": "local_file",
                    "upload_id": "uploads/doc-1.txt",
                }
            ],
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


@pytest.mark.asyncio
async def test_merged_image_refs_over_limit_is_422_not_500(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    uploaded_image: _BoundImage,
    _external_ctx: _ExternalCtx,
) -> None:
    """``image_refs`` (legacy) and ``files[]`` each individually stay within
    their own pydantic ``max_length=64`` — but merged, they can exceed
    ``RunRequest.image_refs``'s own ``max_length=64``. That merge happens by
    hand-constructing ``RunRequest`` outside FastAPI's request-body
    validation path, so an unguarded overflow raises an uncaught pydantic
    ``ValidationError`` (500) rather than a clean 422. 60 legacy refs (<=64)
    + 10 file image entries (<=64) = 70 (>64) reproduces exactly that gap.
    """
    legacy_refs = [
        ImageRef(
            tenant_id=_external_ctx.tenant_id,
            thread_id=uploaded_image.session_id,
            image_id=uuid4(),
            ext=".png",
        ).to_uri()
        for _ in range(60)
    ]
    file_entries = [
        {"type": "image", "transfer_method": "local_file", "upload_id": uploaded_image.uri}
        for _ in range(10)
    ]
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(uploaded_image.session_id),
            "input": "x",
            "mode": "queue",
            "image_refs": legacy_refs,
            "files": file_entries,
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"]


# ---------------------------------------------------------------------------
# Task 11 — files[] document dispatch + path-traversal gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "/etc/passwd",  # absolute path
        "a/b.txt",  # has a "/" but not the uploads/ prefix
        "uploads/../../etc/passwd",  # correct prefix, still traverses — the critical case
        "uploads/a/b.txt",  # multi-level — the leaf itself has a "/"
        "uploads/",  # correct prefix, empty leaf
        "uploads/..",  # correct prefix, leaf is exactly ".."
        "..",
        ".",
        "  ",  # whitespace-only — strips to empty, same as ""
        "x\\y.txt",  # backslash, no uploads/ prefix
        "uploads/a\x00.txt",  # NUL — not in _safe_workspace_name's allowed charset
    ],
)
@pytest.mark.asyncio
async def test_document_path_traversal_rejected(
    external_client: AsyncClient, plain_agent: _PlainAgent, bad: str
) -> None:
    """Every path-traversal / non-``uploads/<safe-leaf>`` shape is rejected
    422 with the structured ``INVALID_FILE_REF`` code — not just a bare
    FastAPI ``{"detail": ...}`` body (this endpoint's contract is the
    enveloped ``{success, data, error}`` shape, same as
    ``TOO_MANY_IMAGE_REFS``).

    修复轮 1:``uploads/../../etc/passwd`` 与 ``uploads/a/b.txt`` 是最关键的
    两条——它们带着"正确"的 ``uploads/`` 前缀,如果闸只检查前缀存在就会被
    放行;必须证明闸校验的是剥掉前缀之后剩下的那段也不含穿越。"""
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "files": [{"type": "document", "transfer_method": "local_file", "upload_id": bad}],
        },
    )
    assert resp.status_code == 422, f"{bad!r} 应被拒: {resp.text}"
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_FILE_REF"


@pytest.mark.asyncio
async def test_document_empty_upload_id_rejected(
    external_client: AsyncClient, plain_agent: _PlainAgent
) -> None:
    """The empty string is rejected too — but earlier than the path gate:
    ``ExternalFileRef.upload_id`` itself has ``min_length=1``, so this one
    never reaches ``_safe_document_name_or_422`` and surfaces the app-wide
    ``/v1/agents/`` validation envelope (``INVALID_REQUEST``) instead of
    ``INVALID_FILE_REF``. Still 422, still enveloped — never a bare
    ``{"detail": ...}``."""
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "x",
            "mode": "queue",
            "files": [{"type": "document", "transfer_method": "local_file", "upload_id": ""}],
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
async def test_document_upload_id_shapes_from_safe_workspace_name_accepted(
    external_client: AsyncClient, plain_agent: _PlainAgent, _external_ctx: _ExternalCtx
) -> None:
    """修复轮 1 —— the gate must accept every shape ``_safe_workspace_name``
    can actually produce. Generated from the *real* function (not hand-typed
    strings) so the gate and the generator are pinned together by the test,
    not by us remembering to keep them in sync."""
    from control_plane.api.uploads import _safe_workspace_name

    real_ids = [
        _safe_workspace_name("报告.docx", ".docx"),  # CJK stem → falls back to a uuid stem
        _safe_workspace_name("a b/c.txt", ".txt"),  # embedded space + "/" in the raw filename
    ]
    for upload_id in real_ids:
        resp = await external_client.post(
            f"/v1/agents/{plain_agent.code}/runs",
            json={
                "user_id": "u1",
                "input": "总结这份文件",
                "mode": "queue",
                "files": [
                    {"type": "document", "transfer_method": "local_file", "upload_id": upload_id}
                ],
            },
        )
        assert resp.status_code == 202, f"{upload_id!r} 应被接受: {resp.text}"
        run_id = UUID(resp.json()["run_id"])
        run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
        assert run is not None
        assert run.enqueued_input is not None
        assert run.enqueued_input["document_names"] == [upload_id]


@pytest.mark.asyncio
async def test_document_ref_lands_in_enqueued_payload(
    external_client: AsyncClient, plain_agent: _PlainAgent, _external_ctx: _ExternalCtx
) -> None:
    """A 202 alone doesn't prove the document name actually reached
    ``RunRequest.document_names`` — read the persisted ``enqueued_input``
    back (queue mode only persists it synchronously; the graph never runs
    in this test) and assert the sanitised name landed exactly.

    修复轮 1:``upload_id`` 改成真实合法形状(``uploads/`` 前缀)——裸文件名
    不再是合法值。"""
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1",
            "input": "总结这份文件",
            "mode": "queue",
            "files": [
                {
                    "type": "document",
                    "transfer_method": "local_file",
                    "upload_id": "uploads/report.pdf",
                }
            ],
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["document_names"] == ["uploads/report.pdf"]


@pytest.mark.asyncio
async def test_mixed_image_and_document_files_dispatch_to_both_channels(
    external_client: AsyncClient,
    vision_agent: _VisionAgent,
    uploaded_image: _BoundImage,
    _external_ctx: _ExternalCtx,
) -> None:
    """A single ``files[]`` array carrying one image and one document must
    fan out into both channels: the image lands in ``image_refs`` (the
    existing multimodal path), the document lands in ``document_names`` —
    independently, in the same request."""
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1",
            "session_id": str(uploaded_image.session_id),
            "input": "看图并总结附件",
            "mode": "queue",
            "files": [
                {"type": "image", "transfer_method": "local_file", "upload_id": uploaded_image.uri},
                {
                    "type": "document",
                    "transfer_method": "local_file",
                    "upload_id": "uploads/summary.docx",
                },
            ],
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["image_refs"] == [uploaded_image.uri]
    assert run.enqueued_input["document_names"] == ["uploads/summary.docx"]


@pytest.mark.asyncio
async def test_no_files_document_names_stays_empty(
    external_client: AsyncClient, plain_agent: _PlainAgent, _external_ctx: _ExternalCtx
) -> None:
    """``files[]`` omitted entirely — the new ``document_names`` plumbing
    must not perturb the pre-Task-11 behaviour of a plain run."""
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={"user_id": "u1", "input": "hello", "mode": "queue"},
    )
    assert resp.status_code == 202, resp.text
    run_id = UUID(resp.json()["run_id"])
    run = await _external_ctx.run_store.get(run_id=run_id, tenant_id=_external_ctx.tenant_id)
    assert run is not None
    assert run.enqueued_input is not None
    assert run.enqueued_input["document_names"] == []
    assert run.enqueued_input["image_refs"] == []
