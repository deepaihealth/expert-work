"""对外附件下载端点测试 —— ``GET /v1/agents/{agent_code}/uploads/{upload_id}``
(附件模型统一 Task 4)。

Fixture shape mirrors ``test_external_artifacts.py``: a service-account
(API-key-shaped) JWT client scoped to one tenant, in-memory
``UserUploadStore`` / ``ImageUploadStore`` / ``TenantUserStore`` (all default
to in-memory backends — ``create_app`` only builds SQL stores when
``settings.store_backend == "sql"``), plus a ``RecordingWorkspaceStore`` for
document bytes and a real ``InMemoryObjectStore`` for image bytes (both
already used the same way by ``test_external_uploads.py`` /
``test_external_artifacts.py``).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.protocol import render_upload_id
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools import (
    RecordingWorkspaceStore,
    SandboxSupervisorError,
    WorkspacePermissionError,
)
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

#: Fixed (not per-test ``uuid4()``) so fixtures and test bodies can both
#: address it — mirrors ``test_external_artifacts.py``'s module-level
#: ``_TENANT_ID``.
_TENANT_ID = uuid4()
_MAX_LIST_LIMIT = 1000


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


def _headers(*, scopes: tuple[str, ...]) -> dict[str, str]:
    """Bearer headers for a service-account (API-key style) principal.

    ``roles=()`` — a service-account principal's RBAC roles come from
    ``scopes`` (``auth/rbac.py:_collect_roles``), so leaving
    ``make_test_jwt``'s human-JWT default ``roles=("admin",)`` in place would
    grant ADMIN via the JWT ``roles`` claim regardless of ``scopes``.
    """
    jwt = make_test_jwt(
        tenant_id=_TENANT_ID,
        subject="third-party-app",
        sub_type="service_account",
        roles=(),
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {jwt}"}


@dataclass
class _Ctx:
    app: object
    uploads: UserUploadStore
    images: ImageUploadStore
    workspace_store: RecordingWorkspaceStore
    object_store: InMemoryObjectStore


@pytest.fixture
def _ctx() -> _Ctx:
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
    )
    workspace_store = RecordingWorkspaceStore()
    app.state.workspace_store = workspace_store  # type: ignore[attr-defined]
    object_store = InMemoryObjectStore()
    app.state.object_store = object_store  # type: ignore[attr-defined]
    return _Ctx(
        app=app,
        uploads=app.state.user_upload_store,  # type: ignore[attr-defined]
        images=app.state.image_upload_store,  # type: ignore[attr-defined]
        workspace_store=workspace_store,
        object_store=object_store,
    )


@pytest.fixture
async def external_client(_ctx: _Ctx) -> AsyncIterator[AsyncClient]:
    """A service-account principal with ``read`` scope — the third-party plane."""
    transport = ASGITransport(app=_ctx.app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers(scopes=("read",))
    ) as client:
        yield client


@pytest.fixture
async def external_client_no_scope(_ctx: _Ctx) -> AsyncIterator[AsyncClient]:
    """Same tenant, same app, but a service-account key minted with zero scopes —
    the ``require("session", "read")`` gate's behavioural cover (mirrors
    ``test_external_artifacts.py``'s fixture of the same name)."""
    transport = ASGITransport(app=_ctx.app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers(scopes=())
    ) as client:
        yield client


@pytest.fixture
def user_store(_ctx: _Ctx) -> TenantUserStore:
    return _ctx.app.state.tenant_user_repo  # type: ignore[no-any-return,attr-defined]


@pytest.fixture
def count_tenant_users(user_store: TenantUserStore) -> Callable[[], Awaitable[int]]:
    """Count this tenant's ``subject_type="user"`` rows — the ground truth for
    "did this request mint a ghost user" (mirrors
    ``test_external_artifacts.py``'s fixture of the same name)."""

    async def _count() -> int:
        rows = await user_store.list_by_tenant(
            _TENANT_ID, subject_type="user", limit=_MAX_LIST_LIMIT
        )
        return len(rows)

    return _count


async def _resolve(user_store: TenantUserStore, user_id: str) -> UUID:
    row = await user_store.resolve(
        tenant_id=_TENANT_ID,
        subject_type="user",
        subject_id=external_subject_id(user_id),
    )
    return row.id


@pytest.fixture
def seed_document(_ctx: _Ctx, user_store: TenantUserStore) -> Callable[..., Awaitable[str]]:
    """Register one document ``user_upload`` row and drop its bytes into the
    workspace-store recorder. Returns the rendered ``upl_<uuid>`` id.

    ``RecordingWorkspaceStore.read_file`` ignores the requested path and
    always returns whatever ``workspace_file`` is currently set to (same
    single-fixture shape ``test_external_artifacts.py``'s
    ``seed_artifact_with_content`` relies on) — fine here because every test
    using this fixture seeds and downloads exactly one document.
    """

    async def _seed(*, user_id: str, filename: str, content: bytes, mime: str) -> str:
        owner = await _resolve(user_store, user_id)
        upload_id = uuid4()
        row = await _ctx.uploads.insert(
            upload_id=upload_id,
            tenant_id=_TENANT_ID,
            user_id=owner,
            thread_id=uuid4(),
            kind="document",
            ref=f"uploads/{filename}",
            mime_type=mime,
            size_bytes=len(content),
            filename=filename,
        )
        _ctx.workspace_store.workspace_file = content
        return render_upload_id(row.id)

    return _seed


@pytest.fixture
def seed_image(_ctx: _Ctx, user_store: TenantUserStore) -> Callable[..., Awaitable[str]]:
    """Register one image ``user_upload`` row backed by a real ``image_upload``
    row + object-store bytes. Returns the rendered ``upl_<uuid>`` id."""

    async def _seed(*, user_id: str, ext: str, content: bytes, mime: str) -> str:
        owner = await _resolve(user_store, user_id)
        thread_id = uuid4()
        image_id = uuid4()
        image_ref = ImageRef(tenant_id=_TENANT_ID, thread_id=thread_id, image_id=image_id, ext=ext)
        await _ctx.images.insert(
            image_id=image_id,
            tenant_id=_TENANT_ID,
            thread_id=thread_id,
            user_id=owner,
            object_key=image_ref.storage_key,
            size_bytes=len(content),
            mime_type=mime,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        await _ctx.object_store.put(image_ref.storage_key, content, content_type=mime)
        upload_id = uuid4()
        row = await _ctx.uploads.insert(
            upload_id=upload_id,
            tenant_id=_TENANT_ID,
            user_id=owner,
            thread_id=thread_id,
            kind="image",
            ref=image_ref.to_uri(),
            mime_type=mime,
            size_bytes=len(content),
            filename=f"{image_id}{ext}",
        )
        return render_upload_id(row.id)

    return _seed


@pytest.fixture
def seed_image_upload_row_only(
    _ctx: _Ctx, user_store: TenantUserStore
) -> Callable[..., Awaitable[str]]:
    """Register a ``user_upload`` row of ``kind="image"`` WITHOUT a matching
    ``image_upload`` row — the "registry row exists, image_upload doesn't"
    404 case. Returns the rendered ``upl_<uuid>`` id."""

    async def _seed(*, user_id: str) -> str:
        owner = await _resolve(user_store, user_id)
        thread_id = uuid4()
        image_ref = ImageRef(
            tenant_id=_TENANT_ID, thread_id=thread_id, image_id=uuid4(), ext=".png"
        )
        upload_id = uuid4()
        row = await _ctx.uploads.insert(
            upload_id=upload_id,
            tenant_id=_TENANT_ID,
            user_id=owner,
            thread_id=thread_id,
            kind="image",
            ref=image_ref.to_uri(),
            mime_type="image/png",
            size_bytes=1,
            filename=f"{image_ref.image_id}.png",
        )
        return render_upload_id(row.id)

    return _seed


@pytest.fixture
def seed_soft_deleted_image(
    _ctx: _Ctx, user_store: TenantUserStore
) -> Callable[..., Awaitable[str]]:
    """Like ``seed_image``, but the ``image_upload`` row is soft-deleted
    (console-side ``delete_image`` shape) right after being created — the
    "console deleted it, retention hasn't reaped the bytes yet" 404 case.
    Returns the rendered ``upl_<uuid>`` id."""

    async def _seed(*, user_id: str, ext: str, content: bytes, mime: str) -> str:
        owner = await _resolve(user_store, user_id)
        thread_id = uuid4()
        image_id = uuid4()
        image_ref = ImageRef(tenant_id=_TENANT_ID, thread_id=thread_id, image_id=image_id, ext=ext)
        await _ctx.images.insert(
            image_id=image_id,
            tenant_id=_TENANT_ID,
            thread_id=thread_id,
            user_id=owner,
            object_key=image_ref.storage_key,
            size_bytes=len(content),
            mime_type=mime,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        await _ctx.object_store.put(image_ref.storage_key, content, content_type=mime)
        flipped = await _ctx.images.soft_delete(
            image_id=image_id, tenant_id=_TENANT_ID, now=datetime.now(UTC)
        )
        assert flipped, "seed_soft_deleted_image: soft_delete did not hit the row it just inserted"
        upload_id = uuid4()
        row = await _ctx.uploads.insert(
            upload_id=upload_id,
            tenant_id=_TENANT_ID,
            user_id=owner,
            thread_id=thread_id,
            kind="image",
            ref=image_ref.to_uri(),
            mime_type=mime,
            size_bytes=len(content),
            filename=f"{image_id}{ext}",
        )
        return render_upload_id(row.id)

    return _seed


@pytest.fixture
def break_workspace_permission(_ctx: _Ctx) -> Callable[[], None]:
    """Make the next ``workspace_store.read_file`` call raise
    ``WorkspacePermissionError`` — the download handler's permission-failure
    path (mirrors ``test_external_artifacts.py``'s fixture of the same
    name)."""

    def _break() -> None:
        _ctx.workspace_store.workspace_file_error = WorkspacePermissionError("boom")

    return _break


@pytest.fixture
def break_workspace_sandbox(_ctx: _Ctx) -> Callable[[], None]:
    """Make the next ``workspace_store.read_file`` call raise the BASE
    ``SandboxSupervisorError`` (not the ``WorkspacePermissionError``
    subclass) — the "content genuinely unavailable" 404 path."""

    def _break() -> None:
        _ctx.workspace_store.workspace_file_error = SandboxSupervisorError("boom")

    return _break


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_document_txt_is_inline_text_plain(external_client, seed_document) -> None:
    upload_id = await seed_document(
        user_id="u-1", filename="notes.txt", content=b"hello world", mime="text/plain"
    )
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.content == b"hello world"


@pytest.mark.asyncio
async def test_download_document_html_forces_attachment(external_client, seed_document) -> None:
    """XSS 红线:可执行内容(HTML)必须 attachment,不能 inline 渲染。"""
    upload_id = await seed_document(
        user_id="u-1",
        filename="page.html",
        content=b"<script>alert(1)</script>",
        mime="text/html",
    )
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_download_document_pdf_content_type_is_upload_time_mime(
    external_client, seed_document
) -> None:
    """spec §一.3:Content-Type 是上传时记录的 MIME(``row.mime_type``),不是
    ``infer_content_type`` 的扩展名猜测——``.pdf`` 不在 ``_artifact_mime`` 的
    任何白名单表里,猜测会落到 ``application/octet-stream``。disposition 仍
    走白名单规则:未识别扩展名 → attachment。"""
    upload_id = await seed_document(
        user_id="u-1", filename="report.pdf", content=b"%PDF-1.4", mime="application/pdf"
    )
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_download_document_csv_content_type_is_upload_time_mime(
    external_client, seed_document
) -> None:
    """同上,反方向的例子:``.csv`` 在 ``_artifact_mime`` 白名单里会被猜成
    ``text/plain``(它落在 text-like 分支),但真实 MIME 是 ``text/csv``。"""
    upload_id = await seed_document(
        user_id="u-1", filename="data.csv", content=b"a,b\n1,2", mime="text/csv"
    )
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 200, resp.text
    # Starlette's ``Response`` appends ``; charset=utf-8`` to any ``text/*``
    # media type automatically — assert the MIME prefix, same convention the
    # ``.pdf`` test above uses.
    assert resp.headers["content-type"].startswith("text/csv")


@pytest.mark.asyncio
async def test_download_image_png_is_inline_with_nosniff(external_client, seed_image) -> None:
    upload_id = await seed_image(
        user_id="u-1", ext=".png", content=b"\x89PNG\r\n", mime="image/png"
    )
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.content == b"\x89PNG\r\n"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_upload_id_is_422(external_client) -> None:
    resp = await external_client.get(
        "/v1/agents/test-agent/uploads/not-a-real-upload-id", params={"user_id": "u-1"}
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_UPLOAD_ID"


@pytest.mark.asyncio
async def test_unknown_user_is_404_and_mints_nothing(external_client, count_tenant_users) -> None:
    before = await count_tenant_users()
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{render_upload_id(uuid4())}",
        params={"user_id": "never-seen-before"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_NOT_FOUND"
    assert await count_tenant_users() == before


@pytest.mark.asyncio
async def test_row_owned_by_a_different_known_user_is_404(external_client, seed_document) -> None:
    """跨用户隔离的真正咬点:user-b 必须是真实存在的用户(否则退化成「未知
    user」短路,测不到 store 层按 user_id 过滤)。"""
    upload_id = await seed_document(
        user_id="user-a", filename="secret.txt", content=b"user-a stuff", mime="text/plain"
    )
    # Seed user-b as a real tenant_user by giving them their own upload.
    await seed_document(
        user_id="user-b", filename="other.txt", content=b"user-b stuff", mime="text/plain"
    )
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "user-b"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "UPLOAD_NOT_FOUND"
    assert resp.content != b"user-a stuff"


@pytest.mark.asyncio
async def test_image_row_present_but_image_upload_missing_is_404(
    external_client, seed_image_upload_row_only
) -> None:
    upload_id = await seed_image_upload_row_only(user_id="u-1")
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_NOT_FOUND"


@pytest.mark.asyncio
async def test_soft_deleted_image_upload_is_404(external_client, seed_soft_deleted_image) -> None:
    """console 侧软删了这张图(``deleted_at`` 已设,字节还没等到 retention
    扫过)——``ImageUploadStore.get`` 不过滤 ``deleted_at``,调用方必须自己判。
    没有这道判,第三方仍能下到一张已被用户删除的图。"""
    upload_id = await seed_soft_deleted_image(
        user_id="u-1", ext=".png", content=b"\x89PNG\r\n", mime="image/png"
    )
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_NOT_FOUND"


@pytest.mark.asyncio
async def test_permission_error_is_500_not_404(
    external_client, seed_document, break_workspace_permission
) -> None:
    """权限失败 → 500,不能和「不存在」合并成 404(W2-BUG-1 教训)。同时锁住
    except 顺序 —— WorkspacePermissionError 是 SandboxSupervisorError 的子类,
    写反顺序这条会红。"""
    upload_id = await seed_document(
        user_id="u-1", filename="report.txt", content=b"x", mime="text/plain"
    )
    break_workspace_permission()
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_CONTENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_sandbox_error_is_404(
    external_client, seed_document, break_workspace_sandbox
) -> None:
    upload_id = await seed_document(
        user_id="u-1", filename="report.txt", content=b"x", mime="text/plain"
    )
    break_workspace_sandbox()
    resp = await external_client.get(
        f"/v1/agents/test-agent/uploads/{upload_id}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_NOT_FOUND"


@pytest.mark.asyncio
async def test_requires_read_scope(external_client_no_scope) -> None:
    resp = await external_client_no_scope.get(
        f"/v1/agents/test-agent/uploads/{render_upload_id(uuid4())}", params={"user_id": "u-1"}
    )
    assert resp.status_code == 403
