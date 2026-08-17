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
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
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
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.workspace_store = workspace_store

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
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
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
        yield _Ctx(client, app, tenant_id, headers, workspace_store)


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
    assert written_path.startswith("uploads/")

    # The unified ``user_upload`` registry row (external附件模型统一, Task 2)
    # must also exist, opaque-id-addressable, pointing at the same workspace path.
    user_upload_row = await ctx.app.state.user_upload_store.get(
        upload_id=parse_upload_id(body["data"]["upload_id"]), tenant_id=ctx.tenant_id
    )
    assert user_upload_row is not None
    assert user_upload_row.kind == "document"
    assert user_upload_row.ref.startswith("uploads/")
    assert user_upload_row.user_id == end_user.id


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
