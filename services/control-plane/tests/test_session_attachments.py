"""``GET /v1/sessions/{thread_id}/attachment`` — console preview / download of one
attachment a conversation's user message carries (B-295).

The ``ref`` is exactly what the transcript chip holds: an image's
``expert_work://image/<tenant>/<thread>/<id><ext>`` reference, or a document's
``uploads/<leaf>`` (resolved server-side inside the conversation's own directory).

Visibility follows the conversation (product decision, 2026-11-07): any employee who
may read the conversation (``session:read``) may open its attachments; a read by
someone other than the owner leaves a ``user:data_view`` audit row.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from PIL import Image

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery
from expert_work.protocol.agent_key import sanitize_agent_key
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools import (
    RecordingWorkspaceStore,
    SandboxSupervisorError,
    WorkspaceFileTooLargeError,
    WorkspacePermissionError,
)
from orchestrator.tools.nas_workspace_store import _WorkspacePathNotFoundError
from orchestrator.tools.sandbox import (
    WorkspaceFileChangedError,
    WorkspaceFileNotFoundError,
    WorkspaceNotAFileError,
    WorkspacePathEscapeError,
)
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin_on,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_OWNER = "user-owner"
_AGENT = "helper"
#: The agent's subtree in the owner's volume (B-50: a cleaned name plus a digest).
_KEY = sanitize_agent_key(_AGENT)


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _headers(subject: str = _OWNER, *, roles: tuple[str, ...] = ("operator",)) -> dict[str, str]:
    token = make_test_jwt(tenant_id=_TENANT, subject=subject, roles=roles)
    return {"Authorization": f"Bearer {token}"}


@dataclass
class World:
    client: AsyncClient
    app: object
    thread_id: UUID
    owner_id: UUID
    objects: InMemoryObjectStore
    workspace: RecordingWorkspaceStore
    audit: InMemoryAuditLogStore

    def url(self, thread_id: UUID | None = None) -> str:
        return f"/v1/sessions/{thread_id or self.thread_id}/attachment"

    async def get(self, ref: str, **kwargs: object) -> Response:
        params = {"ref": ref, **kwargs.pop("params", {})}  # type: ignore[dict-item]
        return await self.client.get(self.url(), params=params, **kwargs)  # type: ignore[arg-type]

    async def seed_image(
        self, *, thread_id: UUID | None = None, mime: str = "image/png", ext: str = ".png"
    ) -> ImageRef:
        """Land an image the way ``_land_image_upload`` does: bytes + registry row."""
        ref = ImageRef(
            tenant_id=_TENANT, thread_id=thread_id or self.thread_id, image_id=uuid4(), ext=ext
        )
        raw = _png()
        await self.objects.put(ref.storage_key, raw, content_type=mime)
        await self.app.state.image_upload_store.insert(  # type: ignore[attr-defined]
            image_id=ref.image_id,
            tenant_id=_TENANT,
            thread_id=ref.thread_id,
            user_id=self.owner_id,
            object_key=ref.storage_key,
            size_bytes=len(raw),
            mime_type=mime,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        return ref

    async def seed_image_in_tenant(self, tenant_id: UUID) -> ImageRef:
        """An image row + bytes in *another* tenant, under this conversation's thread id."""
        ref = ImageRef(tenant_id=tenant_id, thread_id=self.thread_id, image_id=uuid4(), ext=".png")
        raw = _png()
        await self.objects.put(ref.storage_key, raw, content_type="image/png")
        await self.app.state.image_upload_store.insert(  # type: ignore[attr-defined]
            image_id=ref.image_id,
            tenant_id=tenant_id,
            thread_id=self.thread_id,
            user_id=uuid4(),
            object_key=ref.storage_key,
            size_bytes=len(raw),
            mime_type="image/png",
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        return ref

    async def register_upload(self, ref: str, *, thread_id: UUID | None = None) -> UUID:
        """The ``user_upload`` row a conversation-view upload gets (B-281 / external)."""
        row = await self.app.state.user_upload_store.insert(  # type: ignore[attr-defined]
            upload_id=uuid4(),
            tenant_id=_TENANT,
            user_id=self.owner_id,
            thread_id=thread_id or self.thread_id,
            kind="document",
            ref=ref,
            mime_type="application/octet-stream",
            size_bytes=1,
            filename=ref.rsplit("/", 1)[-1],
        )
        return row.id  # type: ignore[no-any-return]

    async def delete_upload(self, upload_id: UUID) -> None:
        """Soft-delete a registry row the way retention / the external delete does."""
        await self.app.state.user_upload_store.soft_delete(  # type: ignore[attr-defined]
            upload_id=upload_id, tenant_id=_TENANT, now=datetime.now(UTC)
        )

    async def put_document(self, ref: str, data: bytes) -> None:
        """A conversation-view upload: bytes in this thread's ``work/`` + its registry row."""
        self.workspace.workspace_file_contents[self.conversation_path(ref)] = data
        await self.register_upload(ref)

    async def views(self) -> list[dict[str, object]]:
        page = await self.audit.query(AuditQuery(tenant_id=_TENANT))
        return [
            {"resource_id": e.resource_id, "actor_id": e.actor_id, **(e.details or {})}
            for e in page.entries
            if e.action.value == "user:data_view"
        ]

    def conversation_path(self, rel: str) -> str:
        return f"agents/{_KEY}/threads/{self.thread_id}/work/{rel}"

    def set_layout(self, layout: str) -> None:
        repo = self.app.state.thread_meta_repo  # type: ignore[attr-defined]
        repo._rows[self.thread_id] = repo._rows[self.thread_id].model_copy(
            update={"workspace_layout": layout}
        )


def _settings() -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


@pytest.fixture
async def world() -> AsyncIterator[World]:
    audit = InMemoryAuditLogStore()
    app = create_app(
        settings=_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit),
        enable_reaper=False,
    )
    objects = InMemoryObjectStore()
    workspace = RecordingWorkspaceStore()
    app.state.object_store = objects
    app.state.workspace_store = workspace
    owner = await app.state.tenant_user_repo.resolve(
        tenant_id=_TENANT, subject_type="user", subject_id=_OWNER
    )
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id,
        tenant_id=_TENANT,
        created_by=_OWNER,
        user_id=owner.id,
        agent_name=_AGENT,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=_headers()
    ) as client:
        yield World(
            client=client,
            app=app,
            thread_id=thread_id,
            owner_id=owner.id,
            objects=objects,
            workspace=workspace,
            audit=audit,
        )


def _assert_safe_headers(resp: Response, *, disposition: str) -> None:
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["content-disposition"].startswith(f"{disposition};")
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]


def _gone(resp: Response) -> None:
    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "ATTACHMENT_GONE"


# ---------------------------------------------------------------------------
# Images — bytes in the object store, registry row in ``image_upload``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_served_inline_with_stored_mime(world: World) -> None:
    ref = await world.seed_image()
    resp = await world.get(ref.to_uri())
    assert resp.status_code == 200, resp.text
    assert resp.content == await world.objects.get(ref.storage_key)
    assert resp.headers["content-type"] == "image/png"
    _assert_safe_headers(resp, disposition="inline")
    assert f"{ref.image_id}.png" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_image_with_non_raster_stored_mime_is_an_attachment(world: World) -> None:
    """Only raster types render inline; anything else (an SVG row, say) is opaque bytes."""
    ref = await world.seed_image(mime="image/svg+xml", ext=".svg")
    resp = await world.get(ref.to_uri())
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/octet-stream"
    _assert_safe_headers(resp, disposition="attachment")


@pytest.mark.asyncio
async def test_soft_deleted_image_is_gone(world: World) -> None:
    ref = await world.seed_image()
    await world.app.state.image_upload_store.soft_delete(  # type: ignore[attr-defined]
        image_id=ref.image_id, tenant_id=_TENANT, now=datetime.now(UTC)
    )
    _gone(await world.get(ref.to_uri()))


@pytest.mark.asyncio
async def test_image_whose_row_was_reaped_is_gone(world: World) -> None:
    ref = await world.seed_image()
    await world.app.state.image_upload_store.hard_delete(image_ids=[ref.image_id])  # type: ignore[attr-defined]
    _gone(await world.get(ref.to_uri()))


@pytest.mark.asyncio
async def test_image_whose_bytes_were_reaped_is_gone(world: World) -> None:
    ref = await world.seed_image()
    await world.objects.delete(ref.storage_key)
    _gone(await world.get(ref.to_uri()))


@pytest.mark.asyncio
async def test_image_of_another_conversation_is_not_served(world: World) -> None:
    """A ref naming another thread (same tenant, another user's) is refused without
    touching that image — the conversation only serves its own attachments."""
    other_thread = uuid4()
    ref = await world.seed_image(thread_id=other_thread)
    _gone(await world.get(ref.to_uri()))


@pytest.mark.asyncio
async def test_image_id_of_another_conversation_under_this_threads_ref_is_not_served(
    world: World,
) -> None:
    """Forging this thread into the ref of another thread's image must not reach it:
    the registry row's thread is checked, not just the ref's."""
    foreign = await world.seed_image(thread_id=uuid4())
    forged = ImageRef(
        tenant_id=_TENANT, thread_id=world.thread_id, image_id=foreign.image_id, ext=".png"
    )
    _gone(await world.get(forged.to_uri()))


@pytest.mark.asyncio
async def test_image_of_another_tenant_under_the_same_thread_id_is_not_served(
    world: World,
) -> None:
    """L2 — a real row + bytes in another tenant, under this very thread id: the lookup
    is scoped to the conversation's tenant, so the ref's own tenant segment reaches
    nothing — and neither does the same image id under this tenant's ref shape."""
    ref = await world.seed_image_in_tenant(uuid4())
    _gone(await world.get(ref.to_uri()))
    forged = ImageRef(
        tenant_id=_TENANT, thread_id=world.thread_id, image_id=ref.image_id, ext=".png"
    )
    _gone(await world.get(forged.to_uri()))


@pytest.mark.asyncio
async def test_malformed_image_ref_is_400(world: World) -> None:
    resp = await world.get("expert_work://image/not-a-uuid/../x.png")
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_image_object_store_failure_is_500_without_detail(world: World) -> None:
    ref = await world.seed_image()

    class _Broken(InMemoryObjectStore):
        async def get(self, key: str) -> bytes:
            from expert_work.runtime.storage import ObjectStoreError

            raise ObjectStoreError(f"denied reading {key}")

    world.app.state.object_store = _Broken()  # type: ignore[attr-defined]
    resp = await world.get(ref.to_uri())
    assert resp.status_code == 500, resp.text
    assert str(ref.image_id) not in resp.text


# ---------------------------------------------------------------------------
# Documents — ``uploads/<leaf>`` resolved in the conversation's own directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_in_conversation_layout_is_served_inline(world: World) -> None:
    world.workspace.workspace_file_contents = {
        f"agents/{_KEY}/uploads/report.pdf": b"%PDF-1.7 a different conversation's file",
    }
    await world.put_document("uploads/report.pdf", b"%PDF-1.7 body")
    resp = await world.get("uploads/report.pdf")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"%PDF-1.7 body"
    assert resp.headers["content-type"] == "application/pdf"
    _assert_safe_headers(resp, disposition="inline")
    assert world.workspace.workspace_reads == [
        (_TENANT, world.owner_id, world.conversation_path("uploads/report.pdf"))
    ]


@pytest.mark.asyncio
async def test_document_in_legacy_layout_reads_the_agent_root(world: World) -> None:
    world.set_layout("agent_root")
    world.workspace.workspace_file_contents = {f"agents/{_KEY}/uploads/notes.txt": b"hello"}
    resp = await world.get("uploads/notes.txt")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"hello"
    assert resp.headers["content-type"] == "text/plain; charset=utf-8"
    _assert_safe_headers(resp, disposition="inline")


@pytest.mark.asyncio
async def test_pdf_without_pdf_magic_is_an_attachment(world: World) -> None:
    await world.put_document("uploads/fake.pdf", b"<html><script>x</script>")
    resp = await world.get("uploads/fake.pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/octet-stream"
    _assert_safe_headers(resp, disposition="attachment")


@pytest.mark.asyncio
async def test_docx_is_an_attachment(world: World) -> None:
    await world.put_document("uploads/合同.docx", b"PK\x03\x04 docx")
    resp = await world.get("uploads/合同.docx")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/octet-stream"
    _assert_safe_headers(resp, disposition="attachment")
    assert "%E5%90%88%E5%90%8C.docx" in resp.headers["content-disposition"]


@pytest.mark.parametrize("leaf", ["page.html", "logo.svg"])
@pytest.mark.asyncio
async def test_active_content_document_is_an_attachment(world: World, leaf: str) -> None:
    world.set_layout("agent_root")
    world.workspace.workspace_file_contents = {f"agents/{_KEY}/uploads/{leaf}": b"<svg/>"}
    resp = await world.get(f"uploads/{leaf}")
    assert resp.status_code == 200, resp.text
    _assert_safe_headers(resp, disposition="attachment")


@pytest.mark.asyncio
async def test_missing_document_is_gone(world: World) -> None:
    """Registered, but the bytes are gone (the store's not-found)."""
    await world.put_document("uploads/other.pdf", b"x")
    await world.register_upload("uploads/report.pdf")
    _gone(await world.get("uploads/report.pdf"))


@pytest.mark.asyncio
async def test_unregistered_conversation_document_falls_back_to_the_path(world: World) -> None:
    """Console uploads into a conversation before B-281 have no registry row at all (prod
    has such conversations since 11-04): path-only — served when the bytes are there,
    410 when they are not."""
    world.workspace.workspace_file_contents = {world.conversation_path("uploads/a.txt"): b"x"}
    resp = await world.get("uploads/a.txt")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"x"
    _gone(await world.get("uploads/missing.txt"))


@pytest.mark.asyncio
async def test_conversation_document_whose_rows_are_all_deleted_is_gone(world: World) -> None:
    """L3 — retention / delete soft-deletes the row and may keep the bytes for a while;
    once every row for this name in this conversation is deleted, it is expired — and the
    bytes are not even read."""
    world.workspace.workspace_file_contents = {world.conversation_path("uploads/a.txt"): b"x"}
    await world.delete_upload(await world.register_upload("uploads/a.txt"))
    _gone(await world.get("uploads/a.txt"))
    assert world.workspace.workspace_reads == []


@pytest.mark.asyncio
async def test_conversation_document_with_a_live_row_is_served(world: World) -> None:
    """One live row is enough — e.g. the name was uploaded again after an earlier row of
    it was deleted."""
    world.workspace.workspace_file_contents = {world.conversation_path("uploads/a.txt"): b"x"}
    await world.delete_upload(await world.register_upload("uploads/a.txt"))
    await world.register_upload("uploads/a.txt")
    assert (await world.get("uploads/a.txt")).status_code == 200


@pytest.mark.asyncio
async def test_rows_of_other_threads_or_names_do_not_decide(world: World) -> None:
    """L4 — only this thread's rows for this very name count. Deleted rows of the same name
    in another thread, or of another name here, leave a file with no row of its own on
    path-only (the path itself is server-built inside this thread's directory)."""
    world.workspace.workspace_file_contents = {world.conversation_path("uploads/a.txt"): b"x"}
    await world.delete_upload(await world.register_upload("uploads/a.txt", thread_id=uuid4()))
    await world.delete_upload(await world.register_upload("uploads/b.txt"))
    resp = await world.get("uploads/a.txt")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"x"


@pytest.mark.asyncio
async def test_legacy_layout_document_needs_no_registry_row(world: World) -> None:
    """Old-view conversations land uploads in the shared agent root, which console uploads
    do not register in (B-281): path-only. Registry rows are not consulted there at all —
    even a deleted row of this thread for the name (an external upload) does not expire a
    file the shared root still holds (decision 2026-11-07: legacy stays path-only)."""
    world.set_layout("agent_root")
    world.workspace.workspace_file_contents = {f"agents/{_KEY}/uploads/a.txt": b"x"}
    assert (await world.get("uploads/a.txt")).status_code == 200
    await world.delete_upload(await world.register_upload("uploads/a.txt"))
    assert (await world.get("uploads/a.txt")).status_code == 200


@pytest.mark.asyncio
async def test_document_too_large_is_413(world: World) -> None:
    await world.register_upload("uploads/report.pdf")
    world.workspace.workspace_file_error = WorkspaceFileTooLargeError("too big")
    resp = await world.get("uploads/report.pdf")
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_document_permission_failure_is_500_without_detail(world: World) -> None:
    await world.register_upload("uploads/report.pdf")
    world.workspace.workspace_file_error = WorkspacePermissionError("/mnt/workspaces/x uid 10002")
    resp = await world.get("uploads/report.pdf")
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text


@pytest.mark.asyncio
async def test_generic_supervisor_error_is_503_not_gone(world: World) -> None:
    """M1 — an unreachable supervisor is not "expired": 503 with a fixed body."""
    await world.register_upload("uploads/report.pdf")
    world.workspace.workspace_file_error = SandboxSupervisorError("supervisor 10.0.0.7 timed out")
    resp = await world.get("uploads/report.pdf")
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "attachment content unavailable"
    assert "10.0.0.7" not in resp.text


@pytest.mark.parametrize(
    "error",
    [
        WorkspaceFileNotFoundError("workspace file not found: 'x'"),
        WorkspaceNotAFileError("workspace path is not a file: 'x'"),
        WorkspacePathEscapeError("workspace path escapes the user root: 'x'"),
        _WorkspacePathNotFoundError("workspace path not found: 'x'"),
    ],
    ids=["file-not-found", "not-a-file", "path-escape", "parent-dir-gone"],
)
@pytest.mark.asyncio
async def test_missing_file_shapes_are_gone(world: World, error: Exception) -> None:
    """M1 — the store's "nothing readable there" answers are 410. ``parent-dir-gone`` is
    the NAS store's answer when the conversation's ``work/`` was reaped whole (B-282)."""
    await world.register_upload("uploads/report.pdf")
    world.workspace.workspace_file_error = error
    _gone(await world.get("uploads/report.pdf"))


@pytest.mark.asyncio
async def test_file_changing_mid_read_is_409(world: World) -> None:
    await world.register_upload("uploads/report.pdf")
    world.workspace.workspace_file_error = WorkspaceFileChangedError("changed while reading")
    resp = await world.get("uploads/report.pdf")
    assert resp.status_code == 409, resp.text


@pytest.mark.parametrize(
    "ref",
    [
        "uploads/../secrets.txt",
        "uploads/..",
        "../etc/passwd",
        "/etc/passwd",
        "uploads/a/b.txt",
        "uploads/a\\b.txt",
        "uploads/",
        "report.pdf",
        "threads/x/work/uploads/report.pdf",
        "uploads/a b.txt",
        "uploads/a\x00.txt",
    ],
)
@pytest.mark.asyncio
async def test_traversal_and_foreign_shapes_are_rejected_before_any_read(
    world: World, ref: str
) -> None:
    resp = await world.get(ref)
    assert resp.status_code == 400, resp.text
    assert world.workspace.workspace_reads == []


# ---------------------------------------------------------------------------
# Authorization — the conversation's own visibility (``session:read``)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
@pytest.mark.asyncio
async def test_non_owner_employee_with_session_read_gets_200_and_is_audited(
    world: World, role: str
) -> None:
    ref = await world.seed_image()
    await world.put_document("uploads/a.txt", b"x")
    for value in (ref.to_uri(), "uploads/a.txt"):
        resp = await world.get(value, headers=_headers(f"colleague-{role}", roles=(role,)))
        assert resp.status_code == 200, resp.text
    views = await world.views()
    assert (
        views
        == [
            {
                "resource_id": str(world.owner_id),
                "actor_id": f"colleague-{role}",
                "view": "conversation_attachment",
                "thread_id": str(world.thread_id),
            }
        ]
        * 2
    )
    # Health data: the audit row names the conversation, never the file.
    assert "a.txt" not in str(views)
    assert str(ref.image_id) not in str(views)


@pytest.mark.asyncio
async def test_owner_reading_their_own_attachment_is_not_a_data_view(world: World) -> None:
    ref = await world.seed_image()
    assert (await world.get(ref.to_uri())).status_code == 200
    assert await world.views() == []


@pytest.mark.asyncio
async def test_a_failed_read_leaves_no_data_view_row(world: World) -> None:
    await world.put_document("uploads/other.txt", b"x")  # the store answers "not found"
    _gone(await world.get("uploads/a.txt", headers=_headers("colleague")))
    assert await world.views() == []


@pytest.mark.asyncio
async def test_caller_without_session_read_is_refused(world: World) -> None:
    ref = await world.seed_image()
    resp = await world.get(ref.to_uri(), headers=_headers("no-role", roles=()))
    assert resp.status_code == 403, resp.text
    assert await world.views() == []


@pytest.mark.asyncio
async def test_unknown_thread_is_404(world: World) -> None:
    resp = await world.client.get(world.url(uuid4()), params={"ref": "uploads/a.txt"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_foreign_tenant_user_naming_this_tenant_is_403(world: World) -> None:
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await world.get("uploads/a.txt", params={"tenant_id": str(_TENANT)}, headers=foreign)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_foreign_tenant_user_without_tenant_id_sees_nothing(world: World) -> None:
    """Without ``tenant_id`` the lookup runs in the caller's home tenant — the thread
    isn't there, so it is the opaque 404, never another tenant's bytes."""
    ref = await world.seed_image()
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), roles=('admin',))}"}
    resp = await world.get(ref.to_uri(), headers=foreign)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_tenant_id_star_is_400(world: World) -> None:
    resp = await world.get("uploads/a.txt", params={"tenant_id": "*"})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_system_admin_reads_through_tenant_passthrough(world: World) -> None:
    ref = await world.seed_image()
    await world.put_document("uploads/report.pdf", b"%PDF-1.4 x")
    sys_headers = await grant_system_admin_on(world.app)
    for value in (ref.to_uri(), "uploads/report.pdf"):
        resp = await world.get(value, params={"tenant_id": str(_TENANT)}, headers=sys_headers)
        assert resp.status_code == 200, resp.text
    assert [v["view"] for v in await world.views()] == ["conversation_attachment"] * 2


async def _mint_api_key(world: World, scopes: list[str]) -> str:
    """A real service-account key (``test_admin_api._mint_key``), so the request gets
    past authentication and the console-only gate is what answers."""
    admin = _headers("key-admin", roles=("admin",))
    sa = await world.client.post(
        "/v1/service_accounts",
        json={"name": f"b295-{uuid4().hex[:8]}", "description": ""},
        headers=admin,
    )
    assert sa.status_code == 201, sa.text
    key = await world.client.post(
        f"/v1/service_accounts/{sa.json()['data']['id']}/api_keys",
        json={"scopes": scopes},
        headers=admin,
    )
    assert key.status_code == 201, key.text
    return str(key.json()["data"]["plaintext"])


@pytest.mark.asyncio
async def test_api_key_is_refused_by_the_console_only_gate(world: World) -> None:
    """L1 — a valid key with read scope authenticates, then the console plane refuses it."""
    ref = await world.seed_image()
    plaintext = await _mint_api_key(world, ["read"])
    resp = await world.get(ref.to_uri(), headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "FORBIDDEN"
    assert "console API is not available to API keys" in resp.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# The session page's artifact download — same visibility as the conversation
# ---------------------------------------------------------------------------


async def _seed_artifact(world: World) -> str:
    await world.app.state.artifact_store.save_version(  # type: ignore[attr-defined]
        tenant_id=_TENANT,
        user_id=world.owner_id,
        agent_key=_KEY,
        name="report.pdf",
        kind="document",
        path_in_workspace="report.pdf",
        created_in_thread=str(world.thread_id),
    )
    world.workspace.workspace_file = b"%PDF-1.4 artifact"
    return f"/v1/sessions/{world.thread_id}/workspace/artifacts/report.pdf/download"


@pytest.mark.parametrize("role", ["viewer", "operator"])
@pytest.mark.asyncio
async def test_non_owner_employee_downloads_the_artifact_and_is_audited(
    world: World, role: str
) -> None:
    url = await _seed_artifact(world)
    resp = await world.client.get(url, headers=_headers(f"colleague-{role}", roles=(role,)))
    assert resp.status_code == 200, resp.text
    assert resp.content == b"%PDF-1.4 artifact"
    assert await world.views() == [
        {
            "resource_id": str(world.owner_id),
            "actor_id": f"colleague-{role}",
            "view": "conversation_artifact",
            "thread_id": str(world.thread_id),
        }
    ]


@pytest.mark.asyncio
async def test_owner_artifact_download_is_not_a_data_view(world: World) -> None:
    url = await _seed_artifact(world)
    assert (await world.client.get(url)).status_code == 200
    assert await world.views() == []


@pytest.mark.asyncio
async def test_artifact_download_without_session_read_is_refused(world: World) -> None:
    url = await _seed_artifact(world)
    resp = await world.client.get(url, headers=_headers("no-role", roles=()))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_artifact_download_cross_tenant_is_still_refused(world: World) -> None:
    url = await _seed_artifact(world)
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), roles=('admin',))}"}
    resp = await world.client.get(url, params={"tenant_id": str(_TENANT)}, headers=foreign)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"
    # Without tenant_id the foreign admin looks in its own tenant: nothing there.
    assert (await world.client.get(url, headers=foreign)).status_code == 404


@pytest.mark.asyncio
async def test_artifact_download_refuses_an_api_key(world: World) -> None:
    url = await _seed_artifact(world)
    plaintext = await _mint_api_key(world, ["read"])
    resp = await world.client.get(url, headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "FORBIDDEN"
