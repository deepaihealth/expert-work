"""Tests for ``/v1/sessions/{thread_id}/uploads`` — Stream J.6 image upload."""

from __future__ import annotations

from collections.abc import AsyncIterator
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from control_plane.app import create_app
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.runtime.storage import InMemoryObjectStore
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


def _png_bytes(*, size: tuple[int, int] = (1, 1), color: int = 0) -> bytes:
    """Generate a real PNG payload — needed since Mini-ADR J-34 sanitisation
    round-trips uploads through Pillow."""
    buf = BytesIO()
    Image.new("RGB", size, color=(color, color, color)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1, 1), color=(0, 0, 0)).save(buf, format="JPEG")
    return buf.getvalue()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "dev",
        "auth_mode": "dev",
        "rate_limit_burst": 10_000,
        "rate_limit_per_second": 10_000.0,
        "oidc_issuer": TEST_ISSUER,
        "oidc_audience": [TEST_AUDIENCE],
        "multimodal_max_image_bytes": 1024,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject='user-a')}"}


Setup = tuple[AsyncClient, UUID, InMemoryObjectStore]


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    """A booted app with an in-memory object store + a pre-seeded thread."""
    object_store = InMemoryObjectStore()
    app = create_app(
        settings=_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    app.state.object_store = object_store
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id, tenant_id=_TENANT, created_by="user-a"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, thread_id, object_store


@pytest.mark.asyncio
async def test_upload_image_returns_image_ref_and_persists_bytes(setup: Setup) -> None:
    client, thread_id, store = setup
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 201
    image_ref = response.json()["image_ref"]
    assert image_ref.startswith(f"expert_work://image/{_TENANT}/{thread_id}/")
    assert image_ref.endswith(".png")

    # The bytes landed under the ADR-0004 prefix. Mini-ADR J-34 round-trips
    # through Pillow (EXIF strip) so we verify a PNG signature lands rather
    # than asserting bit-identical equality with the input.
    keys = await store.list_prefix(f"{_TENANT}/uploads/{thread_id}/")
    assert len(keys) == 1
    stored = await store.get(keys[0])
    assert stored.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_upload_picks_jpg_extension_for_jpeg(setup: Setup) -> None:
    client, thread_id, _ = setup
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.jpeg", _jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 201
    assert response.json()["image_ref"].endswith(".jpg")


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_content_type(setup: Setup) -> None:
    client, thread_id, _ = setup
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftyp", "video/mp4")},
    )
    assert response.status_code == 400
    assert "unsupported content type" in response.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_oversize(setup: Setup) -> None:
    client, thread_id, _ = setup
    too_big = b"x" * (1024 + 1)
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("big.png", too_big, "image/png")},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_upload_rejects_empty_file(setup: Setup) -> None:
    client, thread_id, _ = setup
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_upload_404_for_unknown_thread(setup: Setup) -> None:
    client, _, _ = setup
    response = await client.post(
        f"/v1/sessions/{uuid4()}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_upload_503_when_no_object_store_configured() -> None:
    """Without ``app.state.object_store`` set (lifespan never ran), uploads
    fail loud with 503."""
    app = create_app(
        settings=_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id, tenant_id=_TENANT, created_by="user-a"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        response = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("photo.png", _png_bytes(), "image/png")},
        )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Document upload → workspace (read_document base capability)
# ---------------------------------------------------------------------------


async def _doc_client(
    **settings_overrides: object,
) -> tuple[AsyncClient, UUID, object, object]:
    """A booted app with a fake workspace store wired + a seeded thread."""
    from orchestrator.tools import RecordingWorkspaceStore

    store = RecordingWorkspaceStore()
    app = create_app(
        settings=_settings(**settings_overrides),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    app.state.object_store = InMemoryObjectStore()
    app.state.workspace_store = store
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id, tenant_id=_TENANT, created_by="user-a"
    )
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://cp.test", headers=_headers())
    return client, thread_id, store, app


@pytest.mark.asyncio
async def test_upload_pdf_lands_in_workspace() -> None:
    client, thread_id, store, _ = await _doc_client()
    async with client:
        resp = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("Q3 Report.pdf", b"%PDF-1.4 body", "application/pdf")},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "document"
    assert body["path"].startswith("uploads/")
    assert body["path"].endswith(".pdf")
    # The bytes were written to the caller's workspace volume.
    assert len(store.workspace_writes) == 1
    _tenant, _user, path, data = store.workspace_writes[0]
    assert path == body["path"]
    assert data == b"%PDF-1.4 body"


@pytest.mark.asyncio
async def test_upload_text_document() -> None:
    client, thread_id, _store, _ = await _doc_client()
    async with client:
        resp = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("notes.txt", b"hello world", "text/plain")},
        )
    assert resp.status_code == 201
    assert resp.json()["path"].endswith(".txt")


@pytest.mark.asyncio
async def test_upload_document_extension_from_content_type_not_filename() -> None:
    # A user-supplied filename can't forge the parser extension — it comes from
    # the trusted content type. Path separators in the name are sanitised away.
    client, thread_id, _store, _ = await _doc_client()
    async with client:
        resp = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("../../etc/passwd", b"%PDF-1.4", "application/pdf")},
        )
    assert resp.status_code == 201
    path = resp.json()["path"]
    assert path.startswith("uploads/")
    assert ".." not in path
    assert path.endswith(".pdf")


@pytest.mark.asyncio
async def test_upload_malformed_office_doc_rejected() -> None:
    # A .docx is a ZIP container; non-zip bytes are a malformed office file.
    client, thread_id, _store, _ = await _doc_client()
    async with client:
        resp = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={
                "file": (
                    "memo.docx",
                    b"not a zip",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
    assert resp.status_code == 400
    assert "valid office file" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_document_oversize_rejected() -> None:
    client, thread_id, _store, _ = await _doc_client(document_max_bytes=16)
    async with client:
        resp = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("big.pdf", b"%PDF-" + b"x" * 100, "application/pdf")},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_upload_document_503_without_supervisor(setup: Setup) -> None:
    # The default ``setup`` app has no workspace_store wired → docs 503.
    client, thread_id, _ = setup
    resp = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# W2-BUG-1 — a permission failure on the workspace write must not leak the
# store's exception text (path / uid / mode) into the HTTP response body.
# ``write_file``'s own ``PermissionError`` branch raises
# ``WorkspacePermissionError`` (a ``SandboxSupervisorError`` subclass) since
# ``57fc1680`` — the endpoint's original ``except SandboxSupervisorError as
# exc: ... detail=f"workspace write failed: {exc}"`` caught it too and
# echoed the store's message straight back to the caller.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_document_reports_permission_denied_without_leaking_detail() -> None:
    """store 抛 WorkspacePermissionError → 500(不是笼统的 502),响应体不含
    路径 / uid / mode。500 而非 502 与其余六处 workspace 端点的约定一致——
    权限失败是服务端配置问题,不该混进"上游写失败"的 502 里,这也让"narrow
    分支排在前面"这条要求本身可被 mutation 验证(见下方 M7):两个分支状态码
    不同,分支顺序反了会在状态码上直接可见,不必只靠日志消息去信。
    """
    from orchestrator.tools import WorkspacePermissionError

    client, thread_id, store, _ = await _doc_client()
    store.workspace_write_error = WorkspacePermissionError(
        "PermissionError(13, 'Permission denied'): "
        "'/mnt/workspaces/t-1/u-1/uploads/report.pdf' uid=10002 mode=0o600"
    )
    async with client:
        resp = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("report.pdf", b"%PDF-1.4 body", "application/pdf")},
        )
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_upload_document_still_502s_on_a_generic_supervisor_error() -> None:
    """对照组:普通 SandboxSupervisorError(非权限)仍是 502——既有行为不变。"""
    from orchestrator.tools import SandboxSupervisorError

    client, thread_id, store, _ = await _doc_client()
    store.workspace_write_error = SandboxSupervisorError("supervisor unreachable")
    async with client:
        resp = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("report.pdf", b"%PDF-1.4 body", "application/pdf")},
        )
    assert resp.status_code == 502, resp.text


# ---------------------------------------------------------------------------
# Mini-ADR J-30 (J.6.补强-1) — quota admission
# ---------------------------------------------------------------------------


async def _seed_quota_row(
    app: object,
    *,
    dimension: object,
    limit_value: int,
    burst: int | None = None,
) -> None:
    """Insert one quota row for the default dev tenant."""
    from expert_work.protocol import TenantQuotaPatch

    patch = TenantQuotaPatch(
        dimension=dimension,  # type: ignore[arg-type]
        scope={},
        limit_value=limit_value,
        burst=burst,
    )
    await app.state.tenant_quota_repo.upsert(  # type: ignore[attr-defined]
        tenant_id=_TENANT, patch=patch, updated_by="test"
    )


@pytest.mark.asyncio
async def test_upload_429_when_image_count_quota_exhausted(setup: Setup) -> None:
    """After ``IMAGE_UPLOAD_COUNT_30D`` capacity is consumed the next
    upload returns 429 with the dimension surfaced."""
    from expert_work.protocol import QuotaDimension

    client, thread_id, _ = setup
    await _seed_quota_row(
        client._transport.app,  # type: ignore[attr-defined,union-attr]
        dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D,
        limit_value=2,
        burst=2,
    )
    # 2 uploads within capacity.
    for _ in range(2):
        response = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": ("photo.png", _png_bytes(), "image/png")},
        )
        assert response.status_code == 201
    # 3rd exceeds capacity; slow drip cannot refill in-time.
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["dimension"] == QuotaDimension.IMAGE_UPLOAD_COUNT_30D.value
    assert response.headers["Retry-After"]


@pytest.mark.asyncio
async def test_upload_429_when_image_storage_bytes_exhausted(setup: Setup) -> None:
    """``IMAGE_STORAGE_BYTES`` ceiling is enforced — the upload's
    ``len(raw)`` overrides the default ``cost=1``."""
    from expert_work.protocol import QuotaDimension

    client, thread_id, _ = setup
    # 1x1 PNG round-tripped through Pillow lands around 70 bytes; a
    # ceiling of 100 lets one upload land but denies the second.
    await _seed_quota_row(
        client._transport.app,  # type: ignore[attr-defined,union-attr]
        dimension=QuotaDimension.IMAGE_STORAGE_BYTES,
        limit_value=100,
        burst=None,
    )
    first = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert first.status_code == 201
    second = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(color=128), "image/png")},
    )
    assert second.status_code == 429
    body = second.json()
    assert body["error"]["dimension"] == QuotaDimension.IMAGE_STORAGE_BYTES.value


# ---------------------------------------------------------------------------
# Mini-ADR J-31 (J.6.补强-2) — audit trail
# ---------------------------------------------------------------------------


from expert_work.persistence.audit_log import InMemoryAuditLogStore  # noqa: E402

AuditSetup = tuple[AsyncClient, UUID, InMemoryAuditLogStore, InMemoryObjectStore]


@pytest.fixture
async def audit_setup() -> AsyncIterator[AuditSetup]:
    """Same as :func:`setup` but with an introspectable
    :class:`InMemoryAuditLogStore` so the J.6.补强-2 audit-trail tests
    can assert on the emitted ``image:upload`` rows."""
    from control_plane.audit import build_default_audit_logger

    audit_store = InMemoryAuditLogStore()
    audit_logger = build_default_audit_logger(audit_store)
    object_store = InMemoryObjectStore()
    app = create_app(
        settings=_settings(),
        audit_logger=audit_logger,
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    app.state.object_store = object_store
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id, tenant_id=_TENANT, created_by="user-a"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, thread_id, audit_store, object_store


# ---------------------------------------------------------------------------
# Mini-ADR J-31 (J.6.补强-2) — audit trail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_emits_image_upload_audit_row(audit_setup: AuditSetup) -> None:
    """Successful upload writes a dedicated ``image:upload`` audit row
    with the full byte-trace metadata (size / mime / object_key / sha256)
    that the SESSION_WRITE row does not carry."""
    from expert_work.protocol import AuditAction

    client, thread_id, audit_store, object_store = audit_setup
    raw = _png_bytes()
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", raw, "image/png")},
    )
    assert response.status_code == 201

    # Mini-ADR J-34 strips EXIF before audit/store, so the audit's
    # ``file_size_bytes`` + ``sha256`` reflect the sanitised payload
    # actually persisted — verify they match the stored bytes.
    keys = await object_store.list_prefix(f"{_TENANT}/uploads/{thread_id}/")
    stored = await object_store.get(keys[0])
    import hashlib

    from expert_work.protocol import AuditQuery

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT, limit=50))
    image_upload_rows = [r for r in page.entries if r.action == AuditAction.IMAGE_UPLOAD]
    assert len(image_upload_rows) == 1
    entry = image_upload_rows[0]
    assert entry.resource_type == "image_upload"
    assert entry.tenant_id == _TENANT
    assert entry.details["file_size_bytes"] == len(stored)
    assert entry.details["mime_type"] == "image/png"
    assert entry.details["sha256"] == hashlib.sha256(stored).hexdigest()
    assert entry.details["ext"] == ".png"
    assert entry.details["thread_id"] == str(thread_id)
    assert entry.details["object_key"].startswith(f"{_TENANT}/uploads/{thread_id}/")
    assert entry.details["object_key"].endswith(".png")
    # Subject identity carried for compliance traceability.
    assert entry.details["subject_type"]
    assert entry.details["auth_method"]


@pytest.mark.asyncio
async def test_quota_denial_does_not_emit_image_upload_audit(
    audit_setup: AuditSetup,
) -> None:
    """A 429 from quota admission must NOT emit ``IMAGE_UPLOAD`` (the
    upload didn't happen). The 429 path already emits its own
    ``QUOTA_RATE_LIMIT_DENIED`` row via ``check_admission``."""
    from expert_work.protocol import AuditAction, QuotaDimension

    client, thread_id, audit_store, _ = audit_setup
    await _seed_quota_row(
        client._transport.app,  # type: ignore[attr-defined,union-attr]
        dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D,
        limit_value=1,
        burst=1,
    )
    first = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert first.status_code == 201
    second = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert second.status_code == 429

    from expert_work.protocol import AuditQuery

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT, limit=50))
    image_upload_rows = [r for r in page.entries if r.action == AuditAction.IMAGE_UPLOAD]
    # Only the first (successful) upload should have emitted IMAGE_UPLOAD.
    assert len(image_upload_rows) == 1


# ---------------------------------------------------------------------------
# Mini-ADR J-32 (J.6.补强-3) — lifecycle (image_upload table + DELETE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_registers_row_in_image_upload_store(audit_setup: AuditSetup) -> None:
    """Successful upload writes a row into ``image_upload`` so the
    retention sweep + DELETE endpoint can find it."""
    client, thread_id, _, _ = audit_setup
    response = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 201

    images = client._transport.app.state.image_upload_store  # type: ignore[attr-defined,union-attr]
    rows = await images.list_active_for_thread(tenant_id=_TENANT, thread_id=thread_id)
    assert len(rows) == 1
    row = rows[0]
    # Mini-ADR J-34 — size reflects the EXIF-stripped payload, not the
    # raw upload (raw + sanitised differ by metadata bytes for real images).
    assert row.size_bytes > 0
    assert row.mime_type == "image/png"
    assert row.deleted_at is None
    assert row.object_key.endswith(".png")


@pytest.mark.asyncio
async def test_delete_image_soft_deletes_row_and_emits_audit(audit_setup: AuditSetup) -> None:
    """``DELETE /v1/uploads/{id}`` flips ``deleted_at``, returns 204, and
    writes an ``image:upload`` audit row tagged ``operation=soft_delete``."""
    from expert_work.protocol import AuditAction, AuditQuery

    client, thread_id, audit_store, _ = audit_setup
    upload = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 201
    image_ref = upload.json()["image_ref"]
    image_id = image_ref.rsplit("/", 1)[-1].split(".", 1)[0]

    response = await client.delete(f"/v1/uploads/{image_id}")
    assert response.status_code == 204

    images = client._transport.app.state.image_upload_store  # type: ignore[attr-defined,union-attr]
    active = await images.list_active_for_thread(tenant_id=_TENANT, thread_id=thread_id)
    assert active == []
    raw_row = await images.get(image_id=UUID(image_id), tenant_id=_TENANT)
    assert raw_row is not None and raw_row.deleted_at is not None

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT, limit=50))
    soft_delete_rows = [
        r
        for r in page.entries
        if r.action == AuditAction.IMAGE_UPLOAD and r.details.get("operation") == "soft_delete"
    ]
    assert len(soft_delete_rows) == 1


@pytest.mark.asyncio
async def test_delete_image_404_for_unknown_id(audit_setup: AuditSetup) -> None:
    """Unknown image_id returns 404 — same hides-cross-tenant rule."""
    client, _, _, _ = audit_setup
    response = await client.delete(f"/v1/uploads/{uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_multi_image_upload_round_trips_three_images(audit_setup: AuditSetup) -> None:
    """Mini-ADR J-34 — three uploads in a single thread land EXIF-stripped
    + each gets its own registry row + audit row. Verifies the upload
    pipeline scales beyond a single-image sweet spot."""
    from expert_work.protocol import AuditAction, AuditQuery

    client, thread_id, audit_store, object_store = audit_setup

    # Upload 3 distinct-color PNGs to the same thread.
    for color in (0, 64, 192):
        response = await client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": (f"photo-{color}.png", _png_bytes(color=color), "image/png")},
        )
        assert response.status_code == 201

    # All 3 bytes blobs sit under the thread's prefix.
    keys = await object_store.list_prefix(f"{_TENANT}/uploads/{thread_id}/")
    assert len(keys) == 3
    # All 3 are PNG (signature intact post-sanitisation).
    for key in keys:
        stored = await object_store.get(key)
        assert stored.startswith(b"\x89PNG\r\n\x1a\n")

    # Registry holds 3 active rows for the thread.
    images = client._transport.app.state.image_upload_store  # type: ignore[attr-defined,union-attr]
    rows = await images.list_active_for_thread(tenant_id=_TENANT, thread_id=thread_id)
    assert len(rows) == 3

    # Audit emitted 3 IMAGE_UPLOAD rows.
    page = await audit_store.query(AuditQuery(tenant_id=_TENANT, limit=50))
    image_upload_rows = [
        r
        for r in page.entries
        if r.action == AuditAction.IMAGE_UPLOAD and r.details.get("operation") != "soft_delete"
    ]
    assert len(image_upload_rows) == 3


@pytest.mark.asyncio
async def test_delete_image_is_idempotent(audit_setup: AuditSetup) -> None:
    """Second DELETE on an already soft-deleted image returns 404."""
    client, thread_id, _, _ = audit_setup
    upload = await client.post(
        f"/v1/sessions/{thread_id}/uploads",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    image_ref = upload.json()["image_ref"]
    image_id = image_ref.rsplit("/", 1)[-1].split(".", 1)[0]

    first = await client.delete(f"/v1/uploads/{image_id}")
    assert first.status_code == 204
    second = await client.delete(f"/v1/uploads/{image_id}")
    assert second.status_code == 404
