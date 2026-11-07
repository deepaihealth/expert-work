"""``GET /v1/sessions/{thread_id}/attachment`` — console preview / download of one
attachment a conversation's user message carries (B-295).

The ``ref`` is exactly what the transcript chip holds: an image's
``expert_work://image/<tenant>/<thread>/<id><ext>`` reference, or a document's
``uploads/<leaf>`` (resolved server-side inside the conversation's own directory).
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
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.protocol.agent_key import sanitize_agent_key
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools import (
    RecordingWorkspaceStore,
    SandboxSupervisorError,
    WorkspaceFileTooLargeError,
    WorkspacePermissionError,
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
    app = create_app(
        settings=_settings(), jwt_verifier=build_test_jwt_verifier(), enable_reaper=False
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
        )


def _assert_safe_headers(resp: Response, *, disposition: str) -> None:
    assert resp.headers["x-content-type-options"] == "nosniff"
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
async def test_image_ref_naming_another_tenant_is_not_served(world: World) -> None:
    ref = ImageRef(tenant_id=uuid4(), thread_id=world.thread_id, image_id=uuid4(), ext=".png")
    _gone(await world.get(ref.to_uri()))


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
        world.conversation_path("uploads/report.pdf"): b"%PDF-1.7 body",
        f"agents/{_KEY}/uploads/report.pdf": b"%PDF-1.7 a different conversation's file",
    }
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
    world.workspace.workspace_file_contents = {
        world.conversation_path("uploads/fake.pdf"): b"<html><script>x</script>"
    }
    resp = await world.get("uploads/fake.pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/octet-stream"
    _assert_safe_headers(resp, disposition="attachment")


@pytest.mark.asyncio
async def test_docx_is_an_attachment(world: World) -> None:
    world.workspace.workspace_file_contents = {
        world.conversation_path("uploads/合同.docx"): b"PK\x03\x04 docx"
    }
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
    world.workspace.workspace_file_contents = {world.conversation_path("uploads/other.pdf"): b"x"}
    _gone(await world.get("uploads/report.pdf"))


@pytest.mark.asyncio
async def test_document_too_large_is_413(world: World) -> None:
    world.workspace.workspace_file_error = WorkspaceFileTooLargeError("too big")
    resp = await world.get("uploads/report.pdf")
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_document_permission_failure_is_500_without_detail(world: World) -> None:
    world.workspace.workspace_file_error = WorkspacePermissionError("/mnt/workspaces/x uid 10002")
    resp = await world.get("uploads/report.pdf")
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text


@pytest.mark.asyncio
async def test_generic_supervisor_error_is_gone(world: World) -> None:
    world.workspace.workspace_file_error = SandboxSupervisorError("no such file")
    _gone(await world.get("uploads/report.pdf"))


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
# Authorization — same gate as the other thread-scoped console reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_non_admin_user_gets_404(world: World) -> None:
    ref = await world.seed_image()
    world.workspace.workspace_file_contents = {world.conversation_path("uploads/a.txt"): b"x"}
    for value in (ref.to_uri(), "uploads/a.txt"):
        resp = await world.get(value, headers=_headers("someone-else"))
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "session not found"
    assert world.workspace.workspace_reads == []


@pytest.mark.asyncio
async def test_tenant_admin_may_read_another_users_attachment(world: World) -> None:
    ref = await world.seed_image()
    resp = await world.get(ref.to_uri(), headers=_headers("the-admin", roles=("admin",)))
    assert resp.status_code == 200, resp.text


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
    world.workspace.workspace_file_contents = {
        world.conversation_path("uploads/report.pdf"): b"%PDF-1.4 x"
    }
    sys_headers = await grant_system_admin_on(world.app)
    for value in (ref.to_uri(), "uploads/report.pdf"):
        resp = await world.get(value, params={"tenant_id": str(_TENANT)}, headers=sys_headers)
        assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_api_key_plane_is_refused(world: World) -> None:
    """Console-only: a third-party API key never reaches this endpoint."""
    resp = await world.get(
        "uploads/a.txt", headers={"Authorization": "Bearer ewk_live_notarealkey"}
    )
    assert resp.status_code in {401, 403}, resp.text
