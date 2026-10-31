"""B-262 —— 同名上传不再覆盖(控制台 ``POST /v1/sessions/{t}/uploads``)。

同一智能体的所有对话共用 ``uploads/``(对接方一个用户下跑很多终端客户)。原来客户 B 的
``report.pdf`` 直接顶掉客户 A 的;现在后一个换名(``report-<内容 sha256 前 8 位>.pdf``),
同内容再传一次复用已有文件。对外端点的同款用例在 ``test_external_uploads.py``。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery
from expert_work.protocol.agent_key import sanitize_agent_key
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools import RecordingWorkspaceStore
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_A = b"%PDF-1.4 customer A"
_B = b"%PDF-1.4 customer B -- different"


def _tag(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:8]


class _QuotaRecorder:
    def __init__(self) -> None:
        self.written: list[int] = []

    async def check_upload(self, *, tenant_id: UUID, user_id: UUID, incoming_bytes: int) -> None:
        pass

    async def note_written(self, *, tenant_id: UUID, user_id: UUID, delta_bytes: int) -> None:
        self.written.append(delta_bytes)


class _Console:
    def __init__(self, app: Any, client: AsyncClient, audit: InMemoryAuditLogStore) -> None:
        self.app = app
        self.client = client
        self.audit = audit

    async def thread(self, *, agent_name: str | None = None) -> UUID:
        """一条会话 = 一个终端客户的对话;同一个平台用户、同一个智能体。"""
        thread_id = uuid4()
        await self.app.state.thread_meta_repo.create(
            thread_id=thread_id, tenant_id=_TENANT, created_by="user-a", agent_name=agent_name
        )
        return thread_id

    async def upload(self, thread_id: UUID, raw: bytes, name: str = "report.pdf") -> str:
        resp = await self.client.post(
            f"/v1/sessions/{thread_id}/uploads",
            files={"file": (name, raw, "application/pdf")},
        )
        assert resp.status_code == 201, resp.text
        path = resp.json()["path"]
        assert isinstance(path, str)
        return path

    async def audit_rows(self) -> list[Any]:
        page = await self.audit.query(AuditQuery(tenant_id=_TENANT, limit=50))
        rows = [r for r in page.entries if r.action == AuditAction.DOCUMENT_UPLOAD]
        return sorted(rows, key=lambda r: r.id or 0)


async def _console(store: object) -> tuple[_Console, AsyncClient]:
    audit = InMemoryAuditLogStore()
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        audit_logger=build_default_audit_logger(audit),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    app.state.object_store = InMemoryObjectStore()
    app.state.workspace_store = store
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject='user-a')}"}
    client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=headers
    )
    return _Console(app, client, audit), client


@pytest.mark.asyncio
async def test_same_name_different_bytes_keeps_both_and_renames_the_second() -> None:
    store = RecordingWorkspaceStore()
    console, client = await _console(store)
    async with client:
        first = await console.upload(await console.thread(), _A)
        second = await console.upload(await console.thread(), _B)
        rows = await console.audit_rows()

    assert first == "uploads/report.pdf"
    assert second == f"uploads/report-{_tag(_B)}.pdf"
    # 没绑 agent 的老布局:落在用户根的 ``uploads/``,先来的那份一个字节没动。
    assert store.created_files == {first: _A, second: _B}
    # 审计记的是实际落盘的名字。
    assert [r.resource_id for r in rows] == [first, second]
    assert [r.details["workspace_path"] for r in rows] == [first, second]
    assert [r.details["reused_existing"] for r in rows] == [False, False]


@pytest.mark.asyncio
async def test_agent_scoped_uploads_do_not_overwrite_either() -> None:
    """绑了 agent 的会话:落 ``agents/<key>/uploads/``;回给调用方的 path 不带前缀。"""
    store = RecordingWorkspaceStore()
    console, client = await _console(store)
    key = sanitize_agent_key("support-bot")
    async with client:
        first = await console.upload(await console.thread(agent_name="support-bot"), _A)
        second = await console.upload(await console.thread(agent_name="support-bot"), _B)

    assert (first, second) == ("uploads/report.pdf", f"uploads/report-{_tag(_B)}.pdf")
    assert store.created_files == {f"agents/{key}/{first}": _A, f"agents/{key}/{second}": _B}


@pytest.mark.asyncio
async def test_same_bytes_under_the_same_name_reuses_the_existing_file() -> None:
    """同一份资料再传一次:回同一个名字,不再写一份,不重复记配额,审计标出是复用。"""
    store = RecordingWorkspaceStore()
    console, client = await _console(store)
    quota = _QuotaRecorder()
    console.app.state.workspace_quota_service = quota
    async with client:
        first = await console.upload(await console.thread(), _A)
        again = await console.upload(await console.thread(), _A)
        rows = await console.audit_rows()

    assert again == first == "uploads/report.pdf"
    assert len(store.workspace_writes) == 1
    assert quota.written == [len(_A)]
    assert [r.details["reused_existing"] for r in rows] == [False, True]


@pytest.mark.asyncio
async def test_a_third_upload_with_the_seconds_bytes_reuses_the_renamed_file() -> None:
    store = RecordingWorkspaceStore()
    console, client = await _console(store)
    async with client:
        await console.upload(await console.thread(), _A)
        second = await console.upload(await console.thread(), _B)
        third = await console.upload(await console.thread(), _B)

    assert third == second
    assert len(store.workspace_writes) == 2


@pytest.mark.asyncio
async def test_a_hash_suffix_taken_by_other_bytes_falls_back_to_a_random_name() -> None:
    """内容后缀那个名字被别的内容占了(实际碰不到,这里手工摆出来)—— 照样不覆盖。"""
    store = RecordingWorkspaceStore()
    store.created_files[f"uploads/report-{_tag(_B)}.pdf"] = b"squatter"
    console, client = await _console(store)
    async with client:
        await console.upload(await console.thread(), _A)
        second = await console.upload(await console.thread(), _B)

    assert second not in {"uploads/report.pdf", f"uploads/report-{_tag(_B)}.pdf"}
    assert second.startswith("uploads/report-")
    assert store.created_files[f"uploads/report-{_tag(_B)}.pdf"] == b"squatter"
    assert store.created_files[second] == _B


@pytest.mark.asyncio
async def test_concurrent_same_name_uploads_never_overwrite_each_other(tmp_path: Path) -> None:
    """8 个客户同时传 ``report.pdf``(真 NAS 实现,各自一个线程):8 个名字各不相同,
    每个文件的内容就是传它的那个请求的内容。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    console, client = await _console(store)
    payloads = [f"%PDF-1.4 customer {i}".encode() * 2000 for i in range(8)]
    async with client:
        threads = [await console.thread() for _ in payloads]
        paths = await asyncio.gather(
            *(console.upload(t, raw) for t, raw in zip(threads, payloads, strict=True))
        )

    assert len(set(paths)) == len(payloads)
    (user_root,) = (tmp_path / str(_TENANT)).iterdir()
    for path, raw in zip(paths, payloads, strict=True):
        assert (user_root / path).read_bytes() == raw


@pytest.mark.asyncio
async def test_a_long_chinese_name_still_gets_renamed_instead_of_failing(tmp_path: Path) -> None:
    """82 个汉字的文件名(246 字节)单独放得下 255 字节的上限,加上 ``-<8 位>`` 就超了。
    原名要按字节留出换名的余量,第二份照样换名落盘,不报 502。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    console, client = await _console(store)
    name = "报" * 82 + ".pdf"
    async with client:
        first = await console.upload(await console.thread(), _A, name=name)
        second = await console.upload(await console.thread(), _B, name=name)

    assert first != second
    assert second == f"{first[: -len('.pdf')]}-{_tag(_B)}.pdf"
    for path in (first, second):
        leaf = path.rsplit("/", 1)[-1]
        assert len(leaf.encode()) <= 255
        assert "�" not in leaf  # 没截出半个字
    (user_root,) = (tmp_path / str(_TENANT)).iterdir()
    assert (user_root / first).read_bytes() == _A
    assert (user_root / second).read_bytes() == _B


def test_the_stem_is_cut_on_a_character_boundary_with_room_for_a_random_suffix() -> None:
    from control_plane.api.uploads import _safe_workspace_name, is_safe_document_upload_id

    path = _safe_workspace_name("报" * 82 + "x.docx", ".docx")
    leaf = path.rsplit("/", 1)[-1]
    assert len(leaf.encode()) + len("-") + 12 <= 255
    assert leaf.endswith(".docx")
    assert is_safe_document_upload_id(path)
    # ASCII 短名不受影响。
    assert _safe_workspace_name("Q3 Report.pdf", ".pdf") == "uploads/Q3_Report.pdf"
