"""B-271 —— 上传 90 天:文件按登记行那条会话**当前**的 agent 与布局去删。

B-50 起上传落 ``agents/<key>/uploads/``(老视图),B-262 起对话视图落
``agents/<key>/threads/<会话>/work/uploads/``;此前作业按 ``{用户根}/uploads/<名字>`` 删,
一次都没删到,而旧测试把文件放在旧位置,给坏版本发了合格证。

这里的夹具一律用 control-plane 自己的 ``thread_workspace_path`` 摆文件 —— 上传端点写、
下载端点读都走它,所以文件就在真实位置上,不是测试自己猜的位置。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest

from control_plane.api._workspace_shared import thread_workspace_path
from expert_work.persistence import (
    InMemoryAuditLogStore,
    InMemoryThreadMetaStore,
    InMemoryUserUploadStore,
)
from expert_work.protocol import AuditAction, AuditEntry, AuditResult, ThreadMeta
from retention_cleanup_job import workspace_files
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.workspace_files import upload_relpath

_OLD = datetime.now(UTC) - timedelta(days=100)
_FRESH = datetime.now(UTC) - timedelta(days=1)
_BODY = b"%PDF-1.4 report"
_REF = "uploads/report.pdf"

Layout = Literal["conversation", "agent_root"]


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tenant = uuid4()
        self.user = uuid4()
        self.threads = InMemoryThreadMetaStore()
        self.uploads = InMemoryUserUploadStore()
        self.audit = InMemoryAuditLogStore()

    async def thread(self, *, layout: Layout, agent: str = "support-bot") -> ThreadMeta:
        tid = uuid4()
        await self.threads.create(
            thread_id=tid,
            tenant_id=self.tenant,
            created_by="u",
            user_id=self.user,
            agent_name=agent,
        )
        # 内存实现建出来恒是对话视图;老视图的会话按库里的列值改回去。
        meta = self.threads._rows[tid].model_copy(update={"workspace_layout": layout})
        self.threads._rows[tid] = meta
        return meta

    def place(self, meta: ThreadMeta, data: bytes = _BODY, ref: str = _REF) -> Path:
        """Write ``data`` where the control plane lands ``ref`` for ``meta``."""
        target = self.user_dir / thread_workspace_path(ref, meta)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    @property
    def user_dir(self) -> Path:
        return self.root / str(self.tenant) / str(self.user)

    async def register(
        self,
        thread_id: UUID,
        *,
        created_at: datetime = _OLD,
        data: bytes = _BODY,
        sha256: str | None = "auto",
        ref: str = _REF,
    ) -> UUID:
        upload_id = uuid4()
        await self.uploads.insert(
            upload_id=upload_id,
            tenant_id=self.tenant,
            user_id=self.user,
            thread_id=thread_id,
            kind="document",
            ref=ref,
            mime_type="application/pdf",
            size_bytes=len(data),
            filename=ref.rsplit("/", 1)[-1],
            sha256=hashlib.sha256(data).hexdigest() if sha256 == "auto" else sha256,
        )
        key = (self.tenant, upload_id)
        self.uploads._rows[key] = self.uploads._rows[key].model_copy(
            update={"created_at": created_at}
        )
        return upload_id

    async def deleted(self, upload_id: UUID) -> bool:
        row = await self.uploads.get(upload_id=upload_id, tenant_id=self.tenant)
        assert row is not None
        return row.deleted_at is not None

    def job(self, **overrides: object) -> RetentionCleanupJob:
        kwargs: dict[str, object] = {
            "db_session_factory": lambda: None,
            "user_upload_store": self.uploads,
            "thread_store": self.threads,
            "workspace_root": str(self.root),
            "upload_retention_days": 90,
            "audit_log_store": self.audit,
        }
        kwargs.update(overrides)
        return RetentionCleanupJob(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


# ------------------------------------------------------------ real locations


@pytest.mark.asyncio
async def test_agent_root_layout_removes_the_file_under_the_agent_subtree(world: _World) -> None:
    meta = await world.thread(layout="agent_root")
    real = world.place(meta)
    # 旧作业删的位置:用户根下的 ``uploads/``。这里放一个同名文件,它不是这条登记行的。
    legacy = world.user_dir / _REF
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(_BODY)
    upload = await world.register(meta.thread_id)

    assert await world.job()._sweep_uploads() == (1, 1, 0)

    assert not real.exists()
    assert legacy.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_conversation_layout_removes_the_file_in_the_conversation_work_dir(
    world: _World,
) -> None:
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    assert f"/threads/{meta.thread_id}/work/uploads/" in str(real)
    # B-262 P3 把老对话的上传**复制**进本对话,agent 根下的原件留着 —— 不是这条行管的。
    agent_root_copy = world.place(meta.model_copy(update={"workspace_layout": "agent_root"}))
    upload = await world.register(meta.thread_id)

    assert await world.job()._sweep_uploads() == (1, 1, 0)

    assert not real.exists()
    assert agent_root_copy.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_missing_file_still_expires_the_row(world: _World) -> None:
    meta = await world.thread(layout="conversation")
    upload = await world.register(meta.thread_id)

    assert await world.job()._sweep_uploads() == (1, 0, 0)

    assert await world.deleted(upload)


# ------------------------------------------------------------ reference check


@pytest.mark.asyncio
async def test_a_file_another_live_row_points_at_is_kept(world: _World) -> None:
    """老视图同一 agent 的各对话共用 ``uploads/``:另一条对话 1 天前传了同名同内容(复用了
    同一个文件),它的行没到期 → 文件不删;到期的这行照样软删。"""
    first = await world.thread(layout="agent_root")
    second = await world.thread(layout="agent_root")
    shared = world.place(first)
    assert shared == world.user_dir / thread_workspace_path(_REF, second)
    old = await world.register(first.thread_id)
    fresh = await world.register(second.thread_id, created_at=_FRESH)

    assert await world.job()._sweep_uploads() == (1, 0, 0)

    assert shared.read_bytes() == _BODY
    assert await world.deleted(old)
    assert not await world.deleted(fresh)


@pytest.mark.asyncio
async def test_a_shared_file_goes_once_every_row_pointing_at_it_has_expired(
    world: _World,
) -> None:
    first = await world.thread(layout="agent_root")
    second = await world.thread(layout="agent_root")
    shared = world.place(first)
    rows = [await world.register(t.thread_id) for t in (first, second)]

    assert await world.job()._sweep_uploads() == (2, 1, 0)

    assert not shared.exists()
    assert all([await world.deleted(r) for r in rows])


@pytest.mark.asyncio
async def test_another_expired_row_does_not_hold_the_file_past_the_batch(world: _World) -> None:
    """只有**没到期**的行算引用:另一条也到期了、只是这一批没轮到它,文件照样删。"""
    first = await world.thread(layout="agent_root")
    second = await world.thread(layout="agent_root")
    shared = world.place(first)
    oldest = await world.register(first.thread_id, created_at=_OLD - timedelta(days=1))
    later = await world.register(second.thread_id)

    assert await world.job(batch_size=1)._sweep_uploads() == (1, 1, 0)

    assert not shared.exists()
    assert await world.deleted(oldest)
    assert not await world.deleted(later)


@pytest.mark.asyncio
async def test_a_live_row_with_the_same_ref_at_another_path_does_not_hold_the_file(
    world: _World,
) -> None:
    """同名 ``ref`` 不等于同一个文件:另一条是对话视图,它的文件在自己的 ``work/`` 下。"""
    old_view = await world.thread(layout="agent_root")
    new_view = await world.thread(layout="conversation")
    expiring = world.place(old_view)
    other = world.place(new_view)
    await world.register(old_view.thread_id)
    fresh = await world.register(new_view.thread_id, created_at=_FRESH)

    assert await world.job()._sweep_uploads() == (1, 1, 0)

    assert not expiring.exists()
    assert other.exists()
    assert not await world.deleted(fresh)


@pytest.mark.asyncio
async def test_a_soft_deleted_row_does_not_hold_the_file(world: _World) -> None:
    first = await world.thread(layout="agent_root")
    second = await world.thread(layout="agent_root")
    shared = world.place(first)
    await world.register(first.thread_id)
    gone = await world.register(second.thread_id, created_at=_FRESH)
    await world.uploads.soft_delete(upload_id=gone, tenant_id=world.tenant, now=_FRESH)

    assert await world.job()._sweep_uploads() == (1, 1, 0)

    assert not shared.exists()


# ------------------------------------------------------------ change guard


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now_on_disk",
    [b"%PDF-1.4 report, edited later", b"%PDF-1.4 REPORT"],
    ids=["size-differs", "same-size-content-differs"],
)
async def test_a_file_changed_since_upload_is_kept(world: _World, now_on_disk: bytes) -> None:
    meta = await world.thread(layout="conversation")
    real = world.place(meta, data=now_on_disk)
    upload = await world.register(meta.thread_id)

    assert await world.job()._sweep_uploads() == (1, 0, 0)

    assert real.read_bytes() == now_on_disk
    assert await world.deleted(upload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now_on_disk", "removed"),
    [(b"%PDF-1.4 REPORT", True), (b"%PDF-1.4 report, edited later", False)],
    ids=["same-size-other-bytes-goes", "size-differs-stays"],
)
async def test_a_legacy_row_without_sha256_is_checked_by_size_only(
    world: _World, now_on_disk: bytes, removed: bool
) -> None:
    """列加上之前的老行没有 sha256:只能比大小。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta, data=now_on_disk)
    await world.register(meta.thread_id, sha256=None)

    assert await world.job()._sweep_uploads() == (1, int(removed), 0)

    assert real.exists() is not removed


# ------------------------------------------------------------ thread gone


@pytest.mark.asyncio
async def test_a_row_whose_thread_is_gone_expires_without_touching_files(world: _World) -> None:
    meta = await world.thread(layout="agent_root")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)
    del world.threads._rows[meta.thread_id]

    assert await world.job()._sweep_uploads() == (1, 0, 0)

    assert real.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_without_a_thread_store_nothing_is_touched(world: _World) -> None:
    """路径要按会话算;没接会话库就算不出来 —— 不删文件,行也不标(行是文件唯一的索引)。"""
    meta = await world.thread(layout="agent_root")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)

    assert await world.job(thread_store=None)._sweep_uploads() == (0, 0, 0)

    assert real.exists()
    assert not await world.deleted(upload)


# ------------------------------------------------------------ symlinks


@pytest.mark.asyncio
@pytest.mark.parametrize("segment", ["work", "uploads", "leaf"])
async def test_a_symlink_anywhere_on_the_path_is_not_followed(
    world: _World, tmp_path_factory: pytest.TempPathFactory, segment: str
) -> None:
    """路径上任何一段是软链(对话 ``work/`` 是模型写得到的地方)→ 不跟、不删文件;这是永久性的,
    行照样软删并计数(留着不标会天天重试,按 ``created_at`` 排在最前把整批饿死)。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    outside = tmp_path_factory.mktemp("outside")
    victim = outside / "uploads" / "report.pdf"
    victim.parent.mkdir()
    victim.write_bytes(_BODY)
    if segment == "work":
        work = real.parent.parent
        (real.parent / "report.pdf").unlink()
        real.parent.rmdir()
        work.rmdir()
        os.symlink(outside, work)
    elif segment == "uploads":
        real.unlink()
        real.parent.rmdir()
        os.symlink(victim.parent, real.parent)
    else:
        real.unlink()
        os.symlink(victim, real)
    upload = await world.register(meta.thread_id)

    assert await world.job()._sweep_uploads() == (1, 0, 1)

    assert victim.read_bytes() == _BODY
    assert await world.deleted(upload)


# ------------------------------------------------------------ parity


@pytest.mark.parametrize(
    ("layout", "agent"),
    [
        ("conversation", "support-bot"),
        ("agent_root", "support-bot"),
        ("conversation", None),
        ("agent_root", None),
        # 清洗后与原名不同(非 ASCII、``/``、空格):key 必须是同一个算法算的。
        ("conversation", "客服/助手 v2"),
        ("agent_root", "客服/助手 v2"),
    ],
)
def test_upload_relpath_matches_the_control_plane(layout: Layout, agent: str | None) -> None:
    """作业不 import control-plane(会把 FastAPI / orchestrator 整套拖进来),拼法在两边各写
    一处;这条把两边钉在一起。"""
    meta = ThreadMeta(
        thread_id=uuid4(),
        tenant_id=uuid4(),
        created_by="u",
        agent_name=agent,
        workspace_layout=layout,
    )
    assert upload_relpath(_REF, meta) == thread_workspace_path(_REF, meta)


# ------------------------------------------------------------ review follow-up (M1)


@pytest.mark.asyncio
async def test_a_directory_swapped_for_a_symlink_after_the_check_is_not_followed(
    world: _World, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话里的模型在作业打开 ``work/`` 之后把它换成指向别的用户目录的软链:删的仍是打开的那棵
    目录里的文件,另一个用户同名同大小的文件不动。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    work = real.parent.parent
    victim_root = tmp_path_factory.mktemp("victim")
    victim = victim_root / "uploads" / "report.pdf"
    victim.parent.mkdir()
    victim.write_bytes(_BODY)
    upload = await world.register(meta.thread_id)
    real_open_child = workspace_files._open_child
    swapped: list[str] = []

    def _open_then_swap(parent_fd: int, name: str, flags: int) -> int:
        fd = real_open_child(parent_fd, name, flags)
        if name.endswith("work") and not swapped:
            work.rename(work.with_name("work-moved"))
            os.symlink(victim_root, work)
            swapped.append(name)
        return fd

    monkeypatch.setattr(workspace_files, "_open_child", _open_then_swap)

    assert await world.job()._sweep_uploads() == (1, 1, 0)

    assert swapped
    assert victim.read_bytes() == _BODY
    assert not (work.with_name("work-moved") / "uploads" / "report.pdf").exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_leaf_replaced_after_verification_is_not_deleted(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核对完、删之前,名字被换成另一个文件(不同 inode)→ 不删,行照样软删。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)
    real_unlink = workspace_files.OpenedUpload.unlink

    def _replace_then_unlink(self: workspace_files.OpenedUpload) -> str:
        real.unlink()
        real.write_bytes(b"written by the model later")
        return real_unlink(self)

    monkeypatch.setattr(workspace_files.OpenedUpload, "unlink", _replace_then_unlink)

    assert await world.job()._sweep_uploads() == (1, 0, 0)

    assert real.read_bytes() == b"written by the model later"
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_fifo_leaf_does_not_hang_the_job(world: _World) -> None:
    """叶子被换成 FIFO:不阻塞在 ``open`` 上;不是普通文件 → 不安全,行软删并计数。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    real.unlink()
    os.mkfifo(real)
    upload = await world.register(meta.thread_id)

    sweep = asyncio.ensure_future(world.job()._sweep_uploads())
    done, _ = await asyncio.wait({sweep}, timeout=5)
    if not done:
        # 读端卡在 open 上:开一次写端把它放出来,测试才能干净地失败而不是挂住。
        await asyncio.to_thread(lambda: os.close(os.open(real, os.O_WRONLY)))
        await sweep
        pytest.fail("opening a FIFO leaf blocked the sweep")
    assert sweep.result() == (1, 0, 1)

    assert real.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_symlinked_user_root_is_not_followed(
    world: _World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    meta = await world.thread(layout="agent_root")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    victim = elsewhere / thread_workspace_path(_REF, meta)
    victim.parent.mkdir(parents=True)
    victim.write_bytes(_BODY)
    world.user_dir.parent.mkdir(parents=True)
    os.symlink(elsewhere, world.user_dir)
    await world.register(meta.thread_id)

    assert await world.job()._sweep_uploads() == (1, 0, 1)

    assert victim.read_bytes() == _BODY


# ------------------------------------------------------------ review follow-up (L2)


@pytest.mark.asyncio
async def test_a_row_inserted_between_the_check_and_the_unlink_keeps_the_file(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同内容重传在核对期间插了一行(老视图共用 ``uploads/``):删之前的复查要看到它。"""
    first = await world.thread(layout="agent_root")
    second = await world.thread(layout="agent_root")
    shared = world.place(first)
    await world.register(first.thread_id)
    real_list = world.uploads.list_active_by_ref
    calls: list[int] = []

    async def _list_with_a_late_insert(**kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 2:
            await world.register(second.thread_id, created_at=datetime.now(UTC))
        return await real_list(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(world.uploads, "list_active_by_ref", _list_with_a_late_insert)

    assert await world.job()._sweep_uploads() == (1, 0, 0)

    assert len(calls) == 2
    assert shared.read_bytes() == _BODY


# ------------------------------------------------------------ review follow-up (L6)


@pytest.mark.asyncio
async def test_a_live_candidate_whose_thread_is_gone_does_not_hold_the_file(
    world: _World,
) -> None:
    first = await world.thread(layout="agent_root")
    second = await world.thread(layout="agent_root")
    shared = world.place(first)
    await world.register(first.thread_id)
    await world.register(second.thread_id, created_at=_FRESH)
    del world.threads._rows[second.thread_id]

    assert await world.job()._sweep_uploads() == (1, 1, 0)

    assert not shared.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["other-user", "other-tenant"])
async def test_a_live_row_of_another_user_or_tenant_does_not_hold_the_file(
    world: _World, owner: str
) -> None:
    """另一个用户 / 租户同 agent、同名的没到期行解析出的相对路径字面相同,但落在别人的用户根下。"""
    mine = await world.thread(layout="agent_root")
    real = world.place(mine)
    await world.register(mine.thread_id)
    tenant = uuid4() if owner == "other-tenant" else world.tenant
    user = uuid4()
    theirs = uuid4()
    await world.threads.create(
        thread_id=theirs, tenant_id=tenant, created_by="u", user_id=user, agent_name="support-bot"
    )
    world.threads._rows[theirs] = world.threads._rows[theirs].model_copy(
        update={"workspace_layout": "agent_root"}
    )
    await world.uploads.insert(
        upload_id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        thread_id=theirs,
        kind="document",
        ref=_REF,
        mime_type="application/pdf",
        size_bytes=len(_BODY),
        filename="report.pdf",
        sha256=hashlib.sha256(_BODY).hexdigest(),
    )

    assert await world.job()._sweep_uploads() == (1, 1, 0)

    assert not real.exists()


# ------------------------------------------------------------ review follow-up (L1)


async def _audit_upload(
    world: _World,
    thread_id: UUID,
    *,
    data: bytes,
    occurred_at: datetime,
    ref: str = _REF,
    size: int | None = None,
) -> None:
    await world.audit.append(
        AuditEntry(
            tenant_id=world.tenant,
            actor_type="user",
            actor_id="ext",
            action=AuditAction.DOCUMENT_UPLOAD,
            resource_type="user_workspace",
            resource_id=ref,
            result=AuditResult.SUCCESS,
            details={
                "thread_id": str(thread_id),
                "workspace_path": ref,
                "file_size_bytes": len(data) if size is None else size,
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            occurred_at=occurred_at,
        )
    )


@pytest.mark.asyncio
async def test_a_legacy_row_takes_its_sha256_from_the_upload_audit_row(world: _World) -> None:
    """没有 sha256 的老行:同会话、同名、同大小、贴着登记时间的审计行给出原内容的 sha256 →
    同大小但内容被改过的文件不删。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta, data=b"%PDF-1.4 REPORT")  # same size, other bytes
    upload = await world.register(meta.thread_id, sha256=None)
    await _audit_upload(world, meta.thread_id, data=_BODY, occurred_at=_OLD - timedelta(seconds=2))

    assert await world.job()._sweep_uploads() == (1, 0, 0)

    assert real.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["ambiguous", "outside-window", "other-thread", "other-name", "other-size", "no-audit-store"],
)
async def test_an_unmatched_audit_row_leaves_a_legacy_row_on_size_only(
    world: _World, case: str
) -> None:
    """匹配不唯一 / 不贴着登记时间 / 别的会话 / 别的名字 / 别的大小 → 不用,退回只比大小
    (这里同大小,文件被删)。"""
    meta = await world.thread(layout="conversation")
    on_disk = b"%PDF-1.4 REPORT"
    real = world.place(meta, data=on_disk)
    await world.register(meta.thread_id, sha256=None)
    near = _OLD - timedelta(seconds=2)
    kwargs: dict[str, object] = {}
    expected = (1, 1, 0)
    if case == "ambiguous":
        await _audit_upload(world, meta.thread_id, data=on_disk, occurred_at=near)
    elif case == "outside-window":
        # 同一批里还有一条一小时前登记的老行,批量读的时间窗把这条审计也带了回来;
        # 它离这条行的登记时间一小时,不算这条行的。
        near = _OLD - timedelta(hours=1)
        earlier = await world.thread(layout="conversation")
        world.place(earlier)
        await world.register(earlier.thread_id, sha256=None, created_at=near)
        expected = (2, 2, 0)
    elif case == "other-thread":
        # 同一批里另一条会话的老行:它的审计行在同一次批量读里,但不是这条行的。
        meta = await world.thread(layout="conversation")
        world.place(meta)
        await world.register(meta.thread_id, sha256=None)
        expected = (2, 2, 0)
    elif case == "other-name":
        kwargs["ref"] = "uploads/other.pdf"
    elif case == "other-size":
        kwargs["size"] = len(_BODY) + 1
    await _audit_upload(world, meta.thread_id, data=_BODY, occurred_at=near, **kwargs)  # type: ignore[arg-type]
    overrides = {"audit_log_store": None} if case == "no-audit-store" else {}

    assert await world.job(**overrides)._sweep_uploads() == expected

    assert not real.exists()


# ------------------------------------------------------------ review follow-up (L4)


class _CountingThreads(InMemoryThreadMetaStore):
    def __init__(self, inner: InMemoryThreadMetaStore) -> None:
        super().__init__()
        self._rows = inner._rows
        self.get_many_calls = 0
        self.get_calls = 0

    async def get(self, thread_id: UUID, *, tenant_id: UUID) -> ThreadMeta | None:
        self.get_calls += 1
        return await super().get(thread_id, tenant_id=tenant_id)

    async def get_many(self, thread_ids: object, *, tenant_id: UUID) -> dict[UUID, ThreadMeta]:
        self.get_many_calls += 1
        return await super().get_many(thread_ids, tenant_id=tenant_id)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_threads_are_read_once_per_tenant_per_batch(world: _World) -> None:
    metas = [await world.thread(layout="conversation") for _ in range(3)]
    for meta in metas:
        world.place(meta)
        await world.register(meta.thread_id)
    counting = _CountingThreads(world.threads)

    assert await world.job(thread_store=counting)._sweep_uploads() == (3, 3, 0)

    assert (counting.get_many_calls, counting.get_calls) == (1, 0)


@pytest.mark.asyncio
async def test_one_failing_row_does_not_stop_the_rest(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    metas = [await world.thread(layout="conversation") for _ in range(2)]
    files = [world.place(meta) for meta in metas]
    first = await world.register(metas[0].thread_id, created_at=_OLD - timedelta(days=1))
    second = await world.register(metas[1].thread_id)
    real_soft_delete = world.uploads.soft_delete

    async def _flaky(**kwargs: object) -> bool:
        if kwargs["upload_id"] == first:
            raise RuntimeError("db hiccup")
        return await real_soft_delete(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(world.uploads, "soft_delete", _flaky)

    assert await world.job()._sweep_uploads() == (1, 2, 0)

    assert not any(f.exists() for f in files)
    assert not await world.deleted(first)
    assert await world.deleted(second)


@pytest.mark.asyncio
async def test_a_failing_pass_does_not_raise(world: _World) -> None:
    class _Broken(InMemoryThreadMetaStore):
        async def get_many(self, thread_ids: object, *, tenant_id: UUID) -> dict[UUID, ThreadMeta]:
            raise RuntimeError("db down")

    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)

    assert await world.job(thread_store=_Broken())._sweep_uploads() == (0, 0, 0)

    assert real.exists()
    assert not await world.deleted(upload)
