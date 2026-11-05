"""External file upload — ``POST /v1/agents/{agent_code}/uploads``.

Covers: minting a session when ``session_id`` is omitted (images can't exist
outside a thread — the storage key embeds it) — and that the registry row it
creates really is scoped to the returned session + the declared end user
(not merely "some non-None user"); reusing a supplied session; ownership
scoping (another user's session 404s); the API-key document-upload
regression (the console endpoint 400s a machine caller; this endpoint lands
the document in the *declared end user's* workspace, verified against the
workspace-store recording, not the caller's); the image-upload quota gate
still applying; and rejecting an unsupported content type with the external
plane's ``{success, data, error}`` envelope (not FastAPI's bare ``detail``).
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
from control_plane.api._external_agent_scope import agent_key_for_code
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, CheckResult, QuotaDimension, parse_upload_id
from expert_work.protocol.multimodal import parse_image_ref
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools import RecordingWorkspaceStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


class _AlwaysDenyQuotaService:
    """Stub ``QuotaService`` (structural — matches the ``Protocol``) whose
    ``check`` always denies. Proves the image-upload quota gate
    (``check_admission``) is really wired on the external endpoint: deleting
    that whole block must turn ``test_upload_image_429s_when_quota_denies``
    red."""

    async def check(self, req: object) -> CheckResult:
        del req
        return CheckResult(
            allowed=False,
            blocked_dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D,
            retry_after_s=1,
        )

    async def reserve_tokens(self, req: object) -> object:
        raise NotImplementedError

    async def commit_tokens(self, req: object) -> None:
        raise NotImplementedError

    async def release_tokens(self, reservation_id: object, *, tenant_id: object) -> None:
        raise NotImplementedError


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# The external plane's opaque upload id — see ``expert_work.protocol.user_upload``.
_UPL = re.compile(r"^upl_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
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


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        headers: dict[str, str],
        workspace_store: RecordingWorkspaceStore,
        audit_store: InMemoryAuditLogStore | None = None,
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.workspace_store = workspace_store
        self.audit_store = audit_store or InMemoryAuditLogStore()

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def has_subject(self, subject_id: str) -> bool:
        """Whether a ``tenant_user`` row with this ``subject_id`` exists — the
        ground truth for "did this request mint a ghost user" (same helper
        ``test_external_hardening.py`` uses)."""
        rows = await self.app.state.tenant_user_repo.list_by_tenant(
            self.tenant_id, subject_type="user", limit=MAX_LIST_LIMIT
        )
        return any(row.subject_id == subject_id for row in rows)


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    # ``stub_agent_runtime`` doesn't wire either store: the image branch 503s
    # without an object store, and the document branch 503s without a
    # workspace store (see this task's brief — the fix is a fixture stub,
    # not asserting on the 503).
    app.state.object_store = InMemoryObjectStore()
    workspace_store = RecordingWorkspaceStore()
    app.state.workspace_store = workspace_store

    tenant_id = uuid4()
    # A real API-key caller resolves to a ``service_account`` principal, not
    # a JWT human user — simulate that faithfully (matches
    # ``test_api_key_scope_gate.py``'s ``_key_headers``) since the whole
    # point of this file is proving the endpoint works for a machine caller.
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-external-app",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, workspace_store, audit_store)


@pytest.mark.asyncio
async def test_upload_image_without_session_creates_one(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["type"] == "image"
    assert _UPL.fullmatch(data["upload_id"])
    # Images cannot exist outside a session (the storage key embeds it), so the
    # endpoint mints one and hands it back for the follow-up run call.
    assert data["session_id"]

    # The ``image_upload`` registry row must record the DECLARED end user
    # (resolved from ``user_id``) — not merely "some non-None user" — and
    # the exact thread_id the endpoint minted, which also proves the
    # object-store key really embeds the returned session.
    end_user = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:cust-77"
    )
    session_id = UUID(data["session_id"])
    rows = await ctx.app.state.image_upload_store.list_active_for_thread(
        tenant_id=ctx.tenant_id, thread_id=session_id
    )
    assert len(rows) == 1
    assert rows[0].user_id == end_user.id
    assert rows[0].thread_id == session_id

    # The unified ``user_upload`` registry row (external附件模型统一, Task 2)
    # must also exist, opaque-id-addressable, pointing at the same image ref.
    user_upload_row = await ctx.app.state.user_upload_store.get(
        upload_id=parse_upload_id(data["upload_id"]), tenant_id=ctx.tenant_id
    )
    assert user_upload_row is not None
    assert user_upload_row.kind == "image"
    assert user_upload_row.ref.startswith("expert_work://image/")
    assert user_upload_row.user_id == end_user.id
    # The ref's embedded thread segment must be the row's own thread_id —
    # this is the invariant that makes the download endpoint's image branch
    # able to trust ``parse_image_ref(row.ref)`` without a mismatch ever
    # producing the old bare ``{"detail": "image ref not found"}`` shape.
    assert parse_image_ref(user_upload_row.ref).thread_id == user_upload_row.thread_id
    # B-271 —— 登记行记下落地字节(去 EXIF 之后)的 sha256,与 ``image_upload`` 那行同一个值。
    assert user_upload_row.sha256 == rows[0].sha256


@pytest.mark.asyncio
async def test_upload_reuses_a_supplied_session(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    session_id = first.json()["data"]["session_id"]
    second = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77", "session_id": session_id},
        files={"file": ("b.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["session_id"] == session_id


@pytest.mark.asyncio
async def test_upload_404s_for_another_users_session(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    session_id = first.json()["data"]["session_id"]
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "someone-else", "session_id": session_id},
        files={"file": ("b.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_upload_document_succeeds_for_an_api_key_caller(ctx: _Ctx) -> None:
    """Regression: the console endpoint 400s here because it lands documents in
    the CALLER's workspace and a machine principal has none. The external
    endpoint lands them in the declared end user's workspace instead."""
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["data"]["type"] == "document"
    assert _UPL.fullmatch(body["data"]["upload_id"])

    # The document must land in the DECLARED end user's workspace, not
    # merely "some non-None user" (a wrong-but-non-None user_id would slip
    # past every assertion above this one).
    end_user = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:cust-77"
    )
    assert ctx.workspace_store.workspace_writes, "expected a workspace write"
    written_tenant_id, written_user_id, written_path, _data = ctx.workspace_store.workspace_writes[
        -1
    ]
    assert written_tenant_id == ctx.tenant_id
    assert written_user_id == end_user.id
    # B-50 —— 落盘路径投影到这个 agent 的子树下。``workspace_store`` 是按
    # (tenant, user) 开的,不知道 agent;不投影的话新上传永远落扁平根,而 run
    # 里的 read_document 按 agent 根解析 —— 今天被 PR3 的迁移期读回落兜着,
    # Task 14 摘掉回落就断。
    #
    # 登记行的 ``ref`` **不带前缀**(见下方断言):它是会话 ``/workspace`` 相对的
    # ``uploads/<name>``,也是对接方 run 请求里回传的那个值。两者刻意不同。
    # B-262 P1 —— 新会话是对话视图:落本对话的 ``threads/<会话>/work/uploads/``。
    conv_dir = f"agents/{agent_key_for_code('support-bot')}/threads/{body['data']['session_id']}"
    assert written_path.startswith(f"{conv_dir}/work/uploads/")

    # The unified ``user_upload`` registry row (external附件模型统一, Task 2)
    # must also exist, opaque-id-addressable, pointing at the same workspace path.
    user_upload_row = await ctx.app.state.user_upload_store.get(
        upload_id=parse_upload_id(body["data"]["upload_id"]), tenant_id=ctx.tenant_id
    )
    assert user_upload_row is not None
    assert user_upload_row.kind == "document"
    assert user_upload_row.ref.startswith("uploads/")
    # 登记行的 ref 与落盘路径差的正好是本对话 ``work/`` 的前缀 —— 下载端点就是靠这条
    # 关系(按登记行的会话)把 ref 还原成存储路径的,两边任何一侧漂了都会 404。
    assert written_path == f"{conv_dir}/work/{user_upload_row.ref}"
    assert user_upload_row.user_id == end_user.id
    # B-271 —— 登记行记下落地字节的 sha256:留存作业删文件前用它确认文件没被改过。
    assert user_upload_row.sha256 == hashlib.sha256(b"hello").hexdigest()


@pytest.mark.asyncio
async def test_upload_image_429s_when_quota_denies(ctx: _Ctx) -> None:
    """The image-upload quota gate (``check_admission``) must still apply on
    the external endpoint — the P1 brief requires quota admission stays in
    force; nothing about serving a third party should bypass it."""
    await ctx.seed_agent()
    ctx.app.state.quota_service = _AlwaysDenyQuotaService()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 429, resp.text


@pytest.mark.asyncio
async def test_upload_rejects_an_unsupported_type(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")},
        headers=ctx.headers,
    )
    assert resp.status_code == 400, resp.text
    # External-plane envelope, not FastAPI's bare {"detail": ...} — a
    # third-party SDK parsing error.code must never KeyError here.
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_UPLOAD"
    # Pin the specific guard that fires (not just "some 400") — the
    # allowlist check must be the one that trips, not merely the
    # unrelated extension-lookup fallback that happens to also 400 for
    # this same input (see this file's mutation self-proof notes).
    assert "unsupported content type" in body["error"]["message"]


# ---------------------------------------------------------------------------
# P1 final review, Important I1 — the kill-switch gate must not depend on
# whether the caller supplies ``session_id``
# ---------------------------------------------------------------------------


async def _disable_agent(ctx: _Ctx) -> None:
    """Flip the kill-switch straight through the store + service.

    Going through ``POST /v1/agents/{name}/disable`` would need an employee
    JWT; this fixture's caller is deliberately a machine principal, and the
    gate under test reads the service, not the endpoint.
    """
    await ctx.app.state.agent_disable_repo.set_disabled(
        tenant_id=ctx.tenant_id,
        agent_name="support-bot",
        disabled=True,
        reason="incident-42",
        disabled_by="ops",
    )
    ctx.app.state.agent_disable_service.invalidate(ctx.tenant_id, "support-bot")


@pytest.mark.asyncio
async def test_upload_with_session_id_is_gated_by_the_kill_switch(ctx: _Ctx) -> None:
    """The asymmetry this closes: omitting ``session_id`` routed through
    ``_resolve_session`` (which checks the kill-switch) while supplying one
    routed through ``load_owned_session`` (pure ownership, no kill-switch), so
    a disabled agent still accepted 201s and kept writing into the end user's
    persistent workspace + quota. Design spec §四-6 recommends supplying
    ``session_id`` for follow-up uploads — i.e. the recommended path was the
    ungated one.
    """
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert first.status_code == 201, first.text
    session_id = first.json()["data"]["session_id"]

    await _disable_agent(ctx)

    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77", "session_id": session_id},
        files={"file": ("b.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "AGENT_DISABLED"


@pytest.mark.asyncio
async def test_upload_without_session_id_stays_gated_by_the_kill_switch(ctx: _Ctx) -> None:
    """The control arm — the branch that was already gated must stay gated,
    with the same code, so the fix is "both branches agree", not "the gate
    moved"."""
    await ctx.seed_agent()
    await _disable_agent(ctx)

    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "AGENT_DISABLED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type", "payload", "expected_type"),
    [
        ("a.png", "image/png", _PNG_BYTES, "image"),
        ("notes.txt", "text/plain", b"hello", "document"),
    ],
)
async def test_upload_201_carries_the_full_envelope(
    ctx: _Ctx, filename: str, content_type: str, payload: bytes, expected_type: str
) -> None:
    """P1 final review, I2 — the 201 body was ``{success, data}``: no ``error``
    key at all, while cancel / list / messages / decide all return
    ``{success, data, error}``. A third-party SDK with one envelope parser
    ``KeyError``s on the difference. Both content branches build their own
    response dict, so both are pinned.
    """
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": (filename, payload, content_type)},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"success", "data", "error"}, body
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"]["type"] == expected_type


@pytest.mark.asyncio
async def test_upload_with_a_foreign_session_never_mints_a_ghost_user(ctx: _Ctx) -> None:
    """The other half of Critical C1, on the upload path.

    C1 closed ``load_owned_run`` (cancel / events / decide). This is the same
    defect in the same shape: the ``session_id``-supplied branch resolves the
    end user with the mint-on-use default, so pointing an **existing** session
    id at a never-seen ``user_id`` 404s — correctly — while still writing one
    ``tenant_user`` row per attempt, which then shows up on the user-dimension
    ops page.

    It is structural, not a preference: this branch only ever runs against a
    session that already exists, and an existing session's owner already has a
    ``tenant_user`` row. So there is nothing here to mint — the only row minting
    can create is one for a ``user_id`` that by definition is NOT the owner,
    i.e. exactly the case that must 404. Mint belongs to the branch that omits
    ``session_id`` (``_resolve_session`` genuinely creates the session).
    """
    await ctx.seed_agent()
    owner = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert owner.status_code == 201, owner.text
    session_id = owner.json()["data"]["session_id"]

    subject_id = external_subject_id("never-seen-before")
    assert not await ctx.has_subject(subject_id)

    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data={"user_id": "never-seen-before", "session_id": session_id},
        files={"file": ("b.png", _PNG_BYTES, "image/png")},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"

    assert not await ctx.has_subject(subject_id), (
        "POST .../uploads minted a tenant_user row for a user_id that does not "
        "own the supplied session"
    )


# ---------------------------------------------------------------------------
# B-262 —— 同名上传不再覆盖(对外端点)
# ---------------------------------------------------------------------------


async def _upload_document(
    ctx: _Ctx, raw: bytes, *, user_id: str = "cust-77", session_id: str | None = None
) -> dict[str, Any]:
    """不带 ``session_id``:每次新建一条会话 —— 对接方一个用户下的不同终端客户。"""
    data = {"user_id": user_id} | ({"session_id": session_id} if session_id else {})
    resp = await ctx.client.post(
        "/v1/agents/support-bot/uploads",
        data=data,
        files={"file": ("report.pdf", raw, "application/pdf")},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()["data"]
    return data


async def _upload_row(ctx: _Ctx, data: dict[str, Any]) -> Any:
    upload_id = parse_upload_id(data["upload_id"])
    assert upload_id is not None
    row = await ctx.app.state.user_upload_store.get(upload_id=upload_id, tenant_id=ctx.tenant_id)
    assert row is not None
    return row


def _old_layout(ctx: _Ctx, session_id: str) -> None:
    """把一条会话改成 B-236 之前开的老视图(内存仓库新建的一律是对话视图)。"""
    repo = ctx.app.state.thread_meta_repo
    meta = repo._rows[UUID(session_id)]
    repo._rows[meta.thread_id] = meta.model_copy(update={"workspace_layout": "agent_root"})


@pytest.mark.asyncio
async def test_same_name_uploads_from_two_sessions_each_land_in_their_own_conversation(
    ctx: _Ctx,
) -> None:
    """B-262 P1 —— 客户 A、B 各传一份 ``report.pdf``:各在各的对话目录里、都叫原名;下载按登记行
    的会话找到各自的那一份。同一会话里再传一份不同内容的才换名;登记行、下载、进 run 的
    ``[file attached: …]`` 一律是实际落盘的名字。响应字段一个不变。"""
    import hashlib

    from control_plane.api.agents import ExternalFileRef, resolve_external_files
    from control_plane.api.runs import _build_human_message

    await ctx.seed_agent()
    raw_a, raw_b = b"%PDF-1.4 customer A", b"%PDF-1.4 customer B"
    first = await _upload_document(ctx, raw_a)
    second = await _upload_document(ctx, raw_b)
    third = await _upload_document(ctx, raw_b, session_id=first["session_id"])

    assert set(first) == set(second) == {"upload_id", "session_id", "type", "mime", "size"}
    assert first["upload_id"] != second["upload_id"]
    assert first["session_id"] != second["session_id"]
    rows = [await _upload_row(ctx, d) for d in (first, second, third)]
    renamed = f"report-{hashlib.sha256(raw_b).hexdigest()[:8]}.pdf"
    assert [(r.ref, r.filename) for r in rows] == [
        ("uploads/report.pdf", "report.pdf"),
        ("uploads/report.pdf", "report.pdf"),
        (f"uploads/{renamed}", renamed),
    ]
    key = agent_key_for_code("support-bot")
    a_dir = f"agents/{key}/threads/{first['session_id']}/work/uploads"
    b_dir = f"agents/{key}/threads/{second['session_id']}/work/uploads"
    assert ctx.workspace_store.created_files == {
        f"{a_dir}/report.pdf": raw_a,
        f"{b_dir}/report.pdf": raw_b,
        f"{a_dir}/{renamed}": raw_b,
    }

    for data, raw, name in ((first, raw_a, "report.pdf"), (second, raw_b, "report.pdf")):
        resp = await ctx.client.get(
            f"/v1/agents/support-bot/uploads/{data['upload_id']}",
            params={"user_id": "cust-77"},
            headers=ctx.headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == raw
        assert name in resp.headers["content-disposition"]

    _images, document_names, originals = await resolve_external_files(
        files=[ExternalFileRef(upload_id=third["upload_id"])],
        tenant_id=ctx.tenant_id,
        end_user_id=rows[2].user_id,
        thread_id=UUID(third["session_id"]),
        uploads_store=ctx.app.state.user_upload_store,
        threads=ctx.app.state.thread_meta_repo,
        copier=_copier(ctx),
    )
    assert document_names == [f"uploads/{renamed}"]
    # B-275 —— 模型还要知道它原来叫什么(对接方按附件名给交付件起名)。
    assert originals == {f"uploads/{renamed}": "report.pdf"}
    message = _build_human_message(
        input_text="看一下",
        image_refs=[],
        supports_vision=False,
        document_names=document_names,
        document_original_names=originals,
    )
    assert f"[file attached: uploads/{renamed} (original name: report.pdf)]" in message.content


@pytest.mark.asyncio
async def test_same_bytes_into_one_session_share_one_file(ctx: _Ctx) -> None:
    """同一会话里同内容同名:两个 ``upl_`` id 指向同一个文件,只写一次。"""
    await ctx.seed_agent()
    first = await _upload_document(ctx, b"%PDF-1.4 same")
    second = await _upload_document(ctx, b"%PDF-1.4 same", session_id=first["session_id"])

    rows = [await _upload_row(ctx, d) for d in (first, second)]
    assert first["upload_id"] != second["upload_id"]
    assert rows[0].ref == rows[1].ref == "uploads/report.pdf"
    assert len(ctx.workspace_store.workspace_writes) == 1


@pytest.mark.asyncio
async def test_an_old_layout_session_still_uploads_into_the_agent_root(ctx: _Ctx) -> None:
    """老视图的会话(发布前开的)照旧落 agent 根的 ``uploads/``(各对话共用,P0 的换名规则);
    下载按那条会话的布局找回来。"""
    await ctx.seed_agent()
    first = await _upload_document(ctx, b"%PDF-1.4 first")
    _old_layout(ctx, first["session_id"])
    second = await _upload_document(ctx, b"%PDF-1.4 old", session_id=first["session_id"])

    key = agent_key_for_code("support-bot")
    row = await _upload_row(ctx, second)
    assert ctx.workspace_store.created_files[f"agents/{key}/{row.ref}"] == b"%PDF-1.4 old"
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/uploads/{second['upload_id']}",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"%PDF-1.4 old"


# ---------------------------------------------------------------------------
# B-262 P1 §3 —— run 的 ``files[]`` 引用了别的会话(同一 agent 或另一个 agent)的上传:把文件从
# 那条会话自己的目录复制进本对话的 ``uploads/``(P0 的同名规则),本轮附件名写实际落地名。
# ---------------------------------------------------------------------------


def _copier(ctx: _Ctx) -> Any:
    from control_plane.api.uploads import LandingAccount

    return LandingAccount(
        workspace_store=ctx.workspace_store,
        audit=ctx.app.state.audit_logger,
        actor_id="sa-external-app",
        quota_service=getattr(ctx.app.state, "workspace_quota_service", None),
    )


async def _attach(ctx: _Ctx, upload: dict[str, Any], *, into: str) -> list[str]:
    from control_plane.api.agents import ExternalFileRef, resolve_external_files

    row = await _upload_row(ctx, upload)
    _images, names, _originals = await resolve_external_files(
        files=[ExternalFileRef(upload_id=upload["upload_id"])],
        tenant_id=ctx.tenant_id,
        end_user_id=row.user_id,
        thread_id=UUID(into),
        uploads_store=ctx.app.state.user_upload_store,
        threads=ctx.app.state.thread_meta_repo,
        copier=_copier(ctx),
    )
    return names


async def _foreign_agent_upload(ctx: _Ctx, raw: bytes, *, like: dict[str, Any]) -> dict[str, Any]:
    """同一个终端用户在**另一个 agent** 的会话里传的 ``report.pdf``(直接摆登记行与文件)。"""
    from expert_work.protocol import render_upload_id

    end_user = (await _upload_row(ctx, like)).user_id
    thread = uuid4()
    await ctx.app.state.thread_meta_repo.create(
        thread_id=thread,
        tenant_id=ctx.tenant_id,
        created_by="x",
        user_id=end_user,
        agent_name="other-bot",
    )
    key = agent_key_for_code("other-bot")
    ctx.workspace_store.created_files[f"agents/{key}/threads/{thread}/work/uploads/report.pdf"] = (
        raw
    )
    row = await ctx.app.state.user_upload_store.insert(
        upload_id=uuid4(),
        tenant_id=ctx.tenant_id,
        user_id=end_user,
        thread_id=thread,
        kind="document",
        ref="uploads/report.pdf",
        mime_type="application/pdf",
        size_bytes=len(raw),
        filename="report.pdf",
    )
    return {"upload_id": render_upload_id(row.id), "session_id": str(thread)}


@pytest.mark.asyncio
async def test_an_upload_from_another_session_of_the_same_agent_is_copied_in(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    key = agent_key_for_code("support-bot")
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")  # 会话 B 里已有一份不同内容的 report.pdf

    names = await _attach(ctx, a, into=b["session_id"])

    tag = hashlib.sha256(b"%PDF-1.4 A").hexdigest()[:8]
    assert names == [f"uploads/report-{tag}.pdf"]
    b_dir = f"agents/{key}/threads/{b['session_id']}/work/uploads"
    assert ctx.workspace_store.created_files[f"{b_dir}/report-{tag}.pdf"] == b"%PDF-1.4 A"
    assert ctx.workspace_store.created_files[f"{b_dir}/report.pdf"] == b"%PDF-1.4 B"
    # 再挂一次同一份:同内容复用,不再写。
    writes = len(ctx.workspace_store.workspace_writes)
    assert await _attach(ctx, a, into=b["session_id"]) == names
    assert len(ctx.workspace_store.workspace_writes) == writes
    # 本会话自己的上传:照旧,不复制。
    assert await _attach(ctx, b, into=b["session_id"]) == ["uploads/report.pdf"]
    assert len(ctx.workspace_store.workspace_writes) == writes


@pytest.mark.asyncio
async def test_a_copy_renamed_on_landing_keeps_its_original_name(ctx: _Ctx) -> None:
    """B-275 —— 复制进本会话时撞名换了名的件,原名照样随本轮附件走;本会话自己没换过名的件
    不进「实际名 → 原名」表(那一行与以前逐字相同)。"""
    from control_plane.api.agents import ExternalFileRef, resolve_external_files

    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")  # 会话 B 里已有一份不同内容的 report.pdf

    async def _resolve(upload: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        row = await _upload_row(ctx, upload)
        _images, names, originals = await resolve_external_files(
            files=[ExternalFileRef(upload_id=upload["upload_id"])],
            tenant_id=ctx.tenant_id,
            end_user_id=row.user_id,
            thread_id=UUID(b["session_id"]),
            uploads_store=ctx.app.state.user_upload_store,
            threads=ctx.app.state.thread_meta_repo,
            copier=_copier(ctx),
        )
        return names, originals

    tag = hashlib.sha256(b"%PDF-1.4 A").hexdigest()[:8]
    assert await _resolve(a) == (
        [f"uploads/report-{tag}.pdf"],
        {f"uploads/report-{tag}.pdf": "report.pdf"},
    )
    assert await _resolve(b) == (["uploads/report.pdf"], {})


@pytest.mark.parametrize(
    ("ref", "sha", "original"),
    [
        ("uploads/report-3f9a1c2b.pdf", "3f9a1c2b" + "0" * 56, "report.pdf"),
        ("uploads/报告-3f9a1c2b.pdf", "3f9a1c2b" + "0" * 56, "报告.pdf"),
        ("uploads/v1.2-3f9a1c2b.pdf", "3f9a1c2b" + "0" * 56, "v1.2.pdf"),
        ("uploads/report-3f9a1c2b", "3f9a1c2b" + "0" * 56, "report"),
        # 没换过名。
        ("uploads/report.pdf", "3f9a1c2b" + "0" * 56, None),
        # 名字里碰巧有 8 位十六进制,但不是这份内容的:用户自己起的名字,不是平台换的。
        ("uploads/report-deadbeef.pdf", "3f9a1c2b" + "0" * 56, None),
        # 登记行没有 sha256(B-271 之前的行):认不出,不猜。
        ("uploads/report-3f9a1c2b.pdf", None, None),
        # 只有后缀、没有原名。
        ("uploads/-3f9a1c2b.pdf", "3f9a1c2b" + "0" * 56, None),
    ],
)
def test_original_document_name_inverts_the_rename(
    ref: str, sha: str | None, original: str | None
) -> None:
    from control_plane.api.uploads import original_document_name

    assert original_document_name(ref, sha) == original


@pytest.mark.asyncio
async def test_an_upload_from_another_agents_session_is_copied_from_that_session(
    ctx: _Ctx,
) -> None:
    """另一个 agent 的会话传的件:按登记行的会话找源(那个 agent、那条会话的布局),不按本 agent
    的目录找同名文件 —— 本 agent 根下恰好也有一份 ``uploads/report.pdf``,不许用它。"""
    await ctx.seed_agent()
    key = agent_key_for_code("support-bot")
    mine = await _upload_document(ctx, b"%PDF-1.4 mine")
    ctx.workspace_store.created_files[f"agents/{key}/uploads/report.pdf"] = b"decoy"
    theirs = await _foreign_agent_upload(ctx, b"%PDF-1.4 other agent", like=mine)
    target = await _upload_document(ctx, b"%PDF-1.4 target", session_id=None)

    names = await _attach(ctx, theirs, into=target["session_id"])

    tag = hashlib.sha256(b"%PDF-1.4 other agent").hexdigest()[:8]
    assert names == [f"uploads/report-{tag}.pdf"]
    landed = f"agents/{key}/threads/{target['session_id']}/work/{names[0]}"
    assert ctx.workspace_store.created_files[landed] == b"%PDF-1.4 other agent"


@pytest.mark.asyncio
async def test_an_old_layout_upload_is_copied_into_a_new_conversation(ctx: _Ctx) -> None:
    """源是老视图会话(件在 agent 根):复制进对话视图会话自己的 ``uploads/``。两条都是老视图时
    源与落点是同一个文件,不复制(老视图行为不变)。"""
    await ctx.seed_agent()
    key = agent_key_for_code("support-bot")
    old = await _upload_document(ctx, b"%PDF-1.4 first")
    _old_layout(ctx, old["session_id"])
    in_root = await _upload_document(ctx, b"%PDF-1.4 old", session_id=old["session_id"])
    root_ref = (await _upload_row(ctx, in_root)).ref
    new = await _upload_document(ctx, b"%PDF-1.4 new")

    names = await _attach(ctx, in_root, into=new["session_id"])
    landed = f"agents/{key}/threads/{new['session_id']}/work/{names[0]}"
    assert ctx.workspace_store.created_files[landed] == b"%PDF-1.4 old"

    other_old = await _upload_document(ctx, b"%PDF-1.4 x")
    _old_layout(ctx, other_old["session_id"])
    writes = len(ctx.workspace_store.workspace_writes)
    assert await _attach(ctx, in_root, into=other_old["session_id"]) == [root_ref]
    assert len(ctx.workspace_store.workspace_writes) == writes


@pytest.mark.asyncio
async def test_an_upload_whose_session_is_gone_is_not_found(ctx: _Ctx) -> None:
    from control_plane.api._external import ExternalScopeError

    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    await ctx.app.state.thread_meta_repo.delete(UUID(a["session_id"]), tenant_id=ctx.tenant_id)

    with pytest.raises(ExternalScopeError) as info:
        await _attach(ctx, a, into=b["session_id"])
    # B-262 review —— 与「上传不存在」分开:登记行在,它所在的会话被删了。
    assert (info.value.code, info.value.status_code) == ("UPLOAD_SESSION_DELETED", 404)
    assert "deleted" in info.value.message


# ---------------------------------------------------------------------------
# B-262 review —— 跨会话复制要像上传一样记账(配额 + 审计),出错按上传端点的码回;
# 源会话已删、源文件被改过也要说清楚。
# ---------------------------------------------------------------------------


class _QuotaRecorder:
    def __init__(self, *, full: bool = False) -> None:
        self.full = full
        self.checked: list[int] = []
        self.written: list[int] = []

    async def check_upload(self, *, tenant_id: UUID, user_id: UUID, incoming_bytes: int) -> None:
        from orchestrator.tools.sandbox import WorkspaceQuotaExceededError

        self.checked.append(incoming_bytes)
        if self.full:
            raise WorkspaceQuotaExceededError("full")

    async def note_written(self, *, tenant_id: UUID, user_id: UUID, delta_bytes: int) -> None:
        self.written.append(delta_bytes)


async def _document_audit_rows(ctx: _Ctx) -> list[Any]:
    from expert_work.protocol import AuditAction, AuditQuery

    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=100))
    rows = [r for r in page.entries if r.action == AuditAction.DOCUMENT_UPLOAD]
    return sorted(rows, key=lambda r: r.id or 0)


async def _upload_row_count(ctx: _Ctx) -> int:
    from datetime import UTC, datetime, timedelta

    rows = await ctx.app.state.user_upload_store.list_expired(
        before=datetime.now(UTC) + timedelta(days=1)
    )
    return len(rows)


@pytest.mark.asyncio
async def test_a_copy_is_accounted_and_audited_like_an_upload(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    quota = _QuotaRecorder()
    ctx.app.state.workspace_quota_service = quota
    before_rows = len(await _document_audit_rows(ctx))
    upload_rows = await _upload_row_count(ctx)

    names = await _attach(ctx, a, into=b["session_id"])
    again = await _attach(ctx, a, into=b["session_id"])

    assert again == names
    assert quota.checked == [len(b"%PDF-1.4 A")] * 2
    assert quota.written == [len(b"%PDF-1.4 A")]  # 复用同内容那次不记
    rows = (await _document_audit_rows(ctx))[before_rows:]
    sha = hashlib.sha256(b"%PDF-1.4 A").hexdigest()
    assert [
        (
            r.details["thread_id"],
            r.details["workspace_path"],
            r.details["sha256"],
            r.details["reused_existing"],
            r.details["copied_from_upload_id"],
        )
        for r in rows
    ] == [
        (b["session_id"], names[0], sha, False, a["upload_id"]),
        (b["session_id"], names[0], sha, True, a["upload_id"]),
    ]
    assert await _upload_row_count(ctx) == upload_rows  # 不新建登记行


@pytest.mark.asyncio
async def test_a_copy_into_a_full_workspace_is_refused_like_an_upload(ctx: _Ctx) -> None:
    from control_plane.api._external import ExternalScopeError

    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    ctx.app.state.workspace_quota_service = _QuotaRecorder(full=True)
    writes = len(ctx.workspace_store.workspace_writes)

    with pytest.raises(ExternalScopeError) as info:
        await _attach(ctx, a, into=b["session_id"])
    assert (info.value.code, info.value.status_code) == ("QUOTA_EXCEEDED", 429)
    assert len(ctx.workspace_store.workspace_writes) == writes


@pytest.mark.asyncio
async def test_an_upload_whose_session_is_gone_is_not_found_for_an_old_view_target_either(
    ctx: _Ctx,
) -> None:
    """源会话已删(登记行在 purge 后还留着)、本会话是老视图:同样 404,不按本 agent 根解析
    ``row.ref`` —— P1 起 ``uploads/<名字>`` 只在它自己那段会话的 ``work/`` 里唯一,agent 根下
    同名的可能是别的客户的件(这里摆一份)。"""
    from control_plane.api._external import ExternalScopeError

    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    _old_layout(ctx, b["session_id"])
    key = agent_key_for_code("support-bot")
    ctx.workspace_store.created_files[f"agents/{key}/uploads/report.pdf"] = b"another customer"
    await ctx.app.state.thread_meta_repo.delete(UUID(a["session_id"]), tenant_id=ctx.tenant_id)
    writes = len(ctx.workspace_store.workspace_writes)

    with pytest.raises(ExternalScopeError) as info:
        await _attach(ctx, a, into=b["session_id"])
    assert (info.value.code, info.value.status_code) == ("UPLOAD_SESSION_DELETED", 404)
    assert len(ctx.workspace_store.workspace_writes) == writes


@pytest.mark.asyncio
async def test_a_missing_source_file_is_not_found(ctx: _Ctx) -> None:
    from control_plane.api._external import ExternalScopeError

    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    key = agent_key_for_code("support-bot")
    ctx.workspace_store.created_files.pop(
        f"agents/{key}/threads/{a['session_id']}/work/uploads/report.pdf"
    )
    ctx.workspace_store.workspace_file_contents = {"elsewhere": b"x"}  # 内容表权威:没有即不存在

    with pytest.raises(ExternalScopeError) as info:
        await _attach(ctx, a, into=b["session_id"])
    assert (info.value.code, info.value.status_code) == ("UPLOAD_NOT_FOUND", 404)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("where", "error", "expected"),
    [
        # 读按登记的大小 + 1 封顶(B-262 review):读超了就是文件变了(变大),不是上传太大。
        ("read", "too_large", ("UPLOAD_CHANGED", 409)),
        ("read", "permission", ("UPLOAD_FAILED", 500)),
        ("read", "other", ("UPLOAD_FAILED", 502)),
        ("write", "permission", ("UPLOAD_FAILED", 500)),
        ("write", "other", ("UPLOAD_FAILED", 502)),
    ],
)
async def test_copy_failures_use_the_upload_endpoints_codes(
    ctx: _Ctx, where: str, error: str, expected: tuple[str, int]
) -> None:
    from control_plane.api._external import ExternalScopeError
    from orchestrator.tools.sandbox import (
        SandboxSupervisorError,
        WorkspaceFileTooLargeError,
        WorkspacePermissionError,
    )

    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    exc = {
        "too_large": WorkspaceFileTooLargeError("big"),
        "permission": WorkspacePermissionError("denied"),
        "other": SandboxSupervisorError("boom"),
    }[error]
    if where == "read":
        ctx.workspace_store.workspace_file_error = exc
    else:
        ctx.workspace_store.workspace_write_error = exc

    with pytest.raises(ExternalScopeError) as info:
        await _attach(ctx, a, into=b["session_id"])
    assert (info.value.code, info.value.status_code) == expected


@pytest.mark.asyncio
async def test_a_source_changed_since_upload_is_refused(ctx: _Ctx) -> None:
    """源对话的模型把文件改写了(大小与登记行对不上):不悄悄复制改过的内容。"""
    from control_plane.api._external import ExternalScopeError

    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    key = agent_key_for_code("support-bot")
    source = f"agents/{key}/threads/{a['session_id']}/work/uploads/report.pdf"
    ctx.workspace_store.created_files[source] = b"%PDF-1.4 A, rewritten by the model"
    writes = len(ctx.workspace_store.workspace_writes)

    with pytest.raises(ExternalScopeError) as info:
        await _attach(ctx, a, into=b["session_id"])
    assert (info.value.code, info.value.status_code) == ("UPLOAD_CHANGED", 409)
    assert len(ctx.workspace_store.workspace_writes) == writes


@pytest.mark.asyncio
async def test_the_source_read_is_bounded_by_the_recorded_size(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-262 review —— 源文件按登记的大小 + 1 读,变大 / 被换掉的源不会被整个读进内存再比。"""
    await ctx.seed_agent()
    a = await _upload_document(ctx, b"%PDF-1.4 A")
    b = await _upload_document(ctx, b"%PDF-1.4 B")
    caps: list[int | None] = []
    real = ctx.workspace_store.read_file

    async def spy(**kwargs: Any) -> bytes:
        caps.append(kwargs.get("max_bytes"))
        return await real(**kwargs)

    monkeypatch.setattr(ctx.workspace_store, "read_file", spy)
    await _attach(ctx, a, into=b["session_id"])
    assert caps[0] == len(b"%PDF-1.4 A") + 1
