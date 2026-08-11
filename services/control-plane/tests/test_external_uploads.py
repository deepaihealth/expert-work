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
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, CheckResult, QuotaDimension
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
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
    assert data["upload_id"]
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
    assert resp.json()["data"]["type"] == "document"

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
