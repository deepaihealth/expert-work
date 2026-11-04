"""B-271 —— 上传 90 天:文件按登记行那条会话**当前**的 agent 与布局去删。

B-50 起上传落 ``agents/<key>/uploads/``(老视图),B-262 起对话视图落
``agents/<key>/threads/<会话>/work/uploads/``;此前作业按 ``{用户根}/uploads/<名字>`` 删,
一次都没删到,而旧测试把文件放在旧位置,给坏版本发了合格证。

这里的夹具一律用 control-plane 自己的 ``thread_workspace_path`` 摆文件 —— 上传端点写、
下载端点读都走它,所以文件就在真实位置上,不是测试自己猜的位置。
"""

from __future__ import annotations

import asyncio
import copy
import errno
import hashlib
import logging
import os
import socket
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest

from control_plane.api._workspace_shared import thread_workspace_path
from expert_work.persistence import (
    InMemoryArtifactStore,
    InMemoryAuditLogStore,
    InMemoryThreadMetaStore,
    InMemoryUserUploadStore,
)
from expert_work.protocol import AuditAction, AuditEntry, AuditResult, ThreadMeta
from retention_cleanup_job import job as job_module
from retention_cleanup_job import main as main_module
from retention_cleanup_job import uploads as uploads_module
from retention_cleanup_job import workspace_files
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.main import EXIT_PASS_FAILED, exit_code
from retention_cleanup_job.uploads import UploadSweepTally
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


def _counts(tally: UploadSweepTally) -> tuple[int, int, int]:
    """``(rows_marked, files_removed, files_unsafe)``。"""
    return tally.marked, tally.files_removed, tally.files_unsafe


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

    assert _counts(await world.job()._sweep_uploads()) == (1, 1, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 1, 0)

    assert not real.exists()
    assert agent_root_copy.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_missing_file_still_expires_the_row(world: _World) -> None:
    meta = await world.thread(layout="conversation")
    upload = await world.register(meta.thread_id)

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (2, 1, 0)

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

    assert _counts(await world.job(batch_size=1)._sweep_uploads()) == (1, 1, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 1, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 1, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, int(removed), 0)

    assert real.exists() is not removed


# ------------------------------------------------------------ thread gone


@pytest.mark.asyncio
async def test_a_row_whose_thread_is_gone_expires_without_touching_files(world: _World) -> None:
    meta = await world.thread(layout="agent_root")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)
    del world.threads._rows[meta.thread_id]

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 0)

    assert real.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_without_a_thread_store_nothing_is_touched(world: _World) -> None:
    """路径要按会话算;没接会话库就算不出来 —— 不删文件,行也不标(行是文件唯一的索引)。"""
    meta = await world.thread(layout="agent_root")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)

    assert _counts(await world.job(thread_store=None)._sweep_uploads()) == (0, 0, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 1)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 1, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 0)

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
    assert _counts(sweep.result()) == (1, 0, 1)

    assert real.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_symlinked_user_root_is_not_followed(
    world: _World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``{user}`` 目录沙箱碰不到:它是软链说明环境不对 —— 不跟、整个 pass 失败、行不动。"""
    meta = await world.thread(layout="agent_root")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    victim = elsewhere / thread_workspace_path(_REF, meta)
    victim.parent.mkdir(parents=True)
    victim.write_bytes(_BODY)
    world.user_dir.parent.mkdir(parents=True)
    os.symlink(elsewhere, world.user_dir)
    upload = await world.register(meta.thread_id)
    job = world.job()

    assert _counts(await job._sweep_uploads()) == (0, 0, 0)

    assert job._failed_passes == ["uploads"]
    assert victim.read_bytes() == _BODY
    assert not await world.deleted(upload)


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

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 1, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 1, 0)

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
    sha256: str | None = None,
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
                "sha256": sha256 or hashlib.sha256(data).hexdigest(),
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

    assert _counts(await world.job()._sweep_uploads()) == (1, 0, 0)

    assert real.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "ambiguous",
        "outside-window",
        "other-thread",
        "other-name",
        "other-size",
        "malformed-sha",
        "no-audit-store",
    ],
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
    elif case == "malformed-sha":
        # 不是 64 位小写十六进制(这里是大写):不认。
        kwargs["sha256"] = hashlib.sha256(_BODY).hexdigest().upper()
    await _audit_upload(world, meta.thread_id, data=_BODY, occurred_at=near, **kwargs)  # type: ignore[arg-type]
    overrides = {"audit_log_store": None} if case == "no-audit-store" else {}

    assert _counts(await world.job(**overrides)._sweep_uploads()) == expected

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

    assert _counts(await world.job(thread_store=counting)._sweep_uploads()) == (3, 3, 0)

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

    assert _counts(await world.job()._sweep_uploads()) == (1, 2, 0)

    assert not any(f.exists() for f in files)
    assert not await world.deleted(first)
    assert await world.deleted(second)


def _stub_sql_passes(job: RetentionCleanupJob) -> None:
    """The raw-SQL passes need Postgres; a marker value on the last one proves it still ran."""

    async def _audit_log() -> tuple[int, dict[str, int]]:
        return 0, {}

    async def _zero() -> int:
        return 0

    async def _jwt() -> int:
        return 7

    job._delete_audit_log = _audit_log  # type: ignore[method-assign]
    job._count_unacked_past_retention = _zero  # type: ignore[method-assign]
    job._delete_event_log = _zero  # type: ignore[method-assign]
    job._delete_sandbox_egress_audit = _zero  # type: ignore[method-assign]
    job._delete_finished_memory_writeback_jobs = _zero  # type: ignore[method-assign]
    job._delete_finished_knowledge_tasks = _zero  # type: ignore[method-assign]
    job._delete_orphaned_knowledge_revisions = _zero  # type: ignore[method-assign]
    job._delete_expired_jwt_blacklist = _jwt  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_a_failing_pass_is_reported_and_fails_the_process_after_every_pass_ran(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)

    async def _down(**_kwargs: object) -> list[object]:
        raise RuntimeError("db down")

    monkeypatch.setattr(world.uploads, "list_expired", _down)
    job = world.job()
    _stub_sql_passes(job)

    report = await job.run_once()

    assert report.failed_passes == ("uploads",)
    assert report.jwt_blacklist_deleted == 7  # a later pass still ran
    assert exit_code(report) == EXIT_PASS_FAILED
    assert real.exists()
    assert not await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_clean_run_exits_zero(world: _World) -> None:
    meta = await world.thread(layout="conversation")
    world.place(meta)
    await world.register(meta.thread_id)
    job = world.job()
    _stub_sql_passes(job)

    report = await job.run_once()

    assert (report.failed_passes, report.uploads_expired, report.upload_files_removed) == ((), 1, 1)
    assert exit_code(report) == 0


@pytest.mark.asyncio
async def test_an_unopenable_workspace_root_fails_the_pass_not_the_rows(
    world: _World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """配置的根自己打不开是环境问题:整个 pass 失败,行不标、不算坏路径、不进 7 天兜底。"""
    meta = await world.thread(layout="conversation")
    upload = await world.register(meta.thread_id)
    gone = tmp_path_factory.mktemp("gone")
    gone.rmdir()
    job = world.job(workspace_root=str(gone))
    _stub_sql_passes(job)

    report = await job.run_once()

    assert report.failed_passes == ("uploads",)
    assert (report.upload_files_unsafe, report.upload_rows_abandoned) == (0, 0)
    assert not await world.deleted(upload)


# ------------------------------------------------------------ review round 2 (R-M1)

_as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")


@pytest.mark.asyncio
@_as_root
@pytest.mark.parametrize("target", ["uploads-dir", "file", "unlink-denied"])
async def test_a_path_the_job_cannot_read_expires_the_row_and_keeps_the_file(
    world: _World, target: str
) -> None:
    """作业与沙箱同一个 uid,模型能 ``chmod``:读不了 / 删不了是这一行自己的永久问题 —— 行软删、
    文件不动、计数,不进每天重试。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)
    locked, mode = {
        "uploads-dir": (real.parent, 0o000),
        "file": (real, 0o000),
        "unlink-denied": (real.parent, 0o555),
    }[target]
    locked.chmod(mode)
    try:
        tally = await world.job()._sweep_uploads()
    finally:
        locked.chmod(0o755)

    assert (_counts(tally), tally.rows_failed) == ((1, 0, 1), 0)
    assert real.read_bytes() == _BODY
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_socket_at_the_upload_path_expires_the_row(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """套接字:``open`` 在 Linux 回 ``ENXIO``、macOS 回 ``EOPNOTSUPP`` —— 都是永久的。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    real.unlink()
    monkeypatch.chdir(real.parent)  # AF_UNIX paths are length-limited; bind by a relative name
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(real.name)
        upload = await world.register(meta.thread_id)

        tally = await world.job()._sweep_uploads()
    finally:
        server.close()

    assert (_counts(tally), tally.rows_failed) == ((1, 0, 1), 0)
    assert stat.S_ISSOCK(os.lstat(real).st_mode)
    assert await world.deleted(upload)


def _fail_reading_with(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    def _broken(*_args: object) -> bool:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(workspace_files, "_same_as_registered", _broken)


@pytest.mark.asyncio
async def test_a_transient_io_error_is_retried_tomorrow(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    upload = await world.register(meta.thread_id, created_at=datetime.now(UTC) - timedelta(days=92))
    _fail_reading_with(monkeypatch, errno.EIO)

    tally = await world.job()._sweep_uploads()

    assert (_counts(tally), tally.rows_failed, tally.rows_abandoned) == ((0, 0, 0), 1, 0)
    assert real.exists()
    assert not await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_row_failing_a_week_past_its_expiry_is_abandoned(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兜底:过了到期日 7 天还在暂时性失败,不再等 —— 行软删、文件不动、计数。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    upload = await world.register(meta.thread_id, created_at=datetime.now(UTC) - timedelta(days=98))
    _fail_reading_with(monkeypatch, errno.EIO)

    job = world.job()
    tally = await job._sweep_uploads()

    assert (tally.marked, tally.files_removed, tally.rows_failed, tally.rows_abandoned) == (
        1,
        0,
        0,
        1,
    )
    assert real.exists()
    assert await world.deleted(upload)
    assert job._failed_passes == ["uploads"]  # abandoning is something a human should see


# ------------------------------------------------------------ review round 2 (R-L1 / R-L2)


@pytest.mark.asyncio
async def test_a_failing_audit_lookup_falls_back_to_size_only(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    await world.register(meta.thread_id, sha256=None)

    async def _down(**_kwargs: object) -> list[AuditEntry]:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(world.audit, "list_document_uploads_for_threads", _down)
    job = world.job()
    _stub_sql_passes(job)

    report = await job.run_once()

    assert (report.uploads_expired, report.upload_files_removed, report.failed_passes) == (1, 1, ())
    assert not real.exists()


@pytest.mark.asyncio
async def test_id_lists_are_bound_in_chunks(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """``IN (...)`` 每个 id 一个绑定参数(Postgres 上限 32767):会话预取与审计读都分块。"""
    monkeypatch.setattr(uploads_module, "_IN_CHUNK", 2)
    metas = [await world.thread(layout="conversation") for _ in range(3)]
    for meta in metas:
        world.place(meta)
        await world.register(meta.thread_id, sha256=None)
    counting = _CountingThreads(world.threads)
    audit_calls: list[int] = []
    real_read = world.audit.list_document_uploads_for_threads

    async def _counting_read(**kwargs: object) -> list[AuditEntry]:
        audit_calls.append(len(kwargs["thread_ids"]))  # type: ignore[arg-type]
        return await real_read(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(world.audit, "list_document_uploads_for_threads", _counting_read)

    tally = await world.job(thread_store=counting)._sweep_uploads()

    assert _counts(tally) == (3, 3, 0)
    assert counting.get_many_calls == 2
    assert sorted(audit_calls) == [1, 2]


@pytest.mark.parametrize("code", [0, EXIT_PASS_FAILED])
def test_main_exits_with_the_code_run_once_produced(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    async def _fake_amain() -> int:
        return code

    monkeypatch.setattr(main_module, "_amain", _fake_amain)
    if code == 0:
        main_module.main()
        return
    with pytest.raises(SystemExit) as exited:
        main_module.main()
    assert exited.value.code == EXIT_PASS_FAILED


@pytest.mark.asyncio
async def test_a_failing_orphan_copy_pass_is_reported_too(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(*_args: object) -> int:
        raise RuntimeError("nas scan failed")

    monkeypatch.setattr(job_module, "sweep_orphan_copies", _down)
    job = world.job(artifact_store=InMemoryArtifactStore())
    _stub_sql_passes(job)

    report = await job.run_once()

    assert report.failed_passes == ("orphan_artifact_copies",)
    assert exit_code(report) == EXIT_PASS_FAILED


# ------------------------------------------------------------ review round 3 (breaker)


def _another_user(world: _World) -> _World:
    """同一租户、同一套库与根,换一个用户。"""
    other = copy.copy(world)
    other.user = uuid4()
    return other


@pytest.mark.asyncio
@_as_root
async def test_an_unreadable_tenant_dir_fails_the_pass_and_touches_no_row(world: _World) -> None:
    """``{tenant}`` 沙箱碰不到:读不了是环境问题(作业 uid / NAS 属主),不是这一行的路径坏了。"""
    meta = await world.thread(layout="conversation")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)
    tenant_dir = world.user_dir.parent
    tenant_dir.chmod(0o000)
    try:
        job = world.job()
        tally = await job._sweep_uploads()
    finally:
        tenant_dir.chmod(0o755)

    assert (_counts(tally), job._failed_passes) == ((0, 0, 0), ["uploads"])
    assert real.read_bytes() == _BODY
    assert not await world.deleted(upload)


async def _unlink_denied_users(
    world: _World, count: int, *, extra_rows_for_first: int = 0
) -> list[tuple[_World, Path, UUID]]:
    """``count`` 个用户,各一条到期上传(第一个用户再多 ``extra_rows_for_first`` 条),父目录只读
    (删不了)。"""
    out = []
    for index in range(count):
        who = _another_user(world)
        meta = await who.thread(layout="conversation")
        for n in range(1 + (extra_rows_for_first if index == 0 else 0)):
            ref = _REF if n == 0 else f"uploads/extra-{n}.pdf"
            real = who.place(meta, ref=ref)
            out.append((who, real, await who.register(meta.thread_id, ref=ref)))
    for _, real, _ in out:
        real.parent.chmod(0o555)
    return out


@pytest.mark.asyncio
@_as_root
async def test_unusable_paths_across_many_users_trip_the_breaker(world: _World) -> None:
    """很多用户同时「删不了」更像环境问题:这一轮一条都不放弃、pass 记失败;同一轮里正常的
    删除照常。"""
    denied = await _unlink_denied_users(world, 4)
    fine = await world.thread(layout="conversation")
    fine_file = world.place(fine)
    fine_upload = await world.register(fine.thread_id)
    try:
        job = world.job()
        tally = await job._sweep_uploads()
    finally:
        for _, real, _ in denied:
            real.parent.chmod(0o755)

    assert (_counts(tally), tally.rows_held) == ((1, 1, 0), 4)
    assert job._failed_passes == ["uploads"]
    for who, real, upload in denied:
        assert real.exists()
        assert not await who.deleted(upload)
    assert not fine_file.exists()
    assert await world.deleted(fine_upload)


@pytest.mark.asyncio
@_as_root
async def test_unusable_paths_within_the_breaker_still_expire(world: _World) -> None:
    """3 个用户以内(模型弄坏自己工作区的量级)照常放弃:行软删、文件不动、不算 pass 失败。
    熔断按**用户**数,不按行数:一个用户名下多条坏行照样放弃。"""
    denied = await _unlink_denied_users(world, 3, extra_rows_for_first=2)
    try:
        job = world.job()
        tally = await job._sweep_uploads()
    finally:
        for _, real, _ in denied:
            real.parent.chmod(0o755)

    assert (_counts(tally), tally.rows_held) == ((5, 0, 5), 0)
    assert job._failed_passes == []
    for who, real, upload in denied:
        assert real.exists()
        assert await who.deleted(upload)


@pytest.mark.asyncio
async def test_a_code_bug_in_every_old_row_is_held_not_abandoned(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归让每一行都抛、而且都过了到期日 7 天:涉及的用户多 → 一条都不放弃,pass 记失败。"""
    rows = []
    for _ in range(4):
        who = _another_user(world)
        meta = await who.thread(layout="conversation")
        real = who.place(meta)
        rows.append((who, real, await who.register(meta.thread_id)))

    async def _regression(*_args: object) -> bool:
        raise TypeError("regression")

    monkeypatch.setattr(uploads_module.UploadSweep, "_release_file", _regression)
    job = world.job()

    tally = await job._sweep_uploads()

    assert (tally.marked, tally.rows_abandoned, tally.rows_held) == (0, 0, 4)
    assert job._failed_passes == ["uploads"]
    for who, real, upload in rows:
        assert real.exists()
        assert not await who.deleted(upload)


@pytest.mark.asyncio
async def test_transient_failures_log_once_per_class_with_a_count(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recent = datetime.now(UTC) - timedelta(days=92)
    for _ in range(2):
        meta = await world.thread(layout="conversation")
        world.place(meta)
        await world.register(meta.thread_id, created_at=recent)
    _fail_reading_with(monkeypatch, errno.EIO)

    with caplog.at_level(logging.WARNING, logger="retention_cleanup_job.uploads"):
        tally = await world.job()._sweep_uploads()

    assert tally.rows_failed == 2
    records = [r for r in caplog.records if "retention.upload_row" in r.getMessage()]
    assert [r.getMessage().split(" sample_upload_id")[0] for r in records] == [
        "retention.upload_rows_failed class=OSError/EIO count=2"
    ]
    assert records[0].exc_info is not None
