"""B-271 —— 上传 90 天:文件按登记行那条会话**当前**的 agent 与布局去删。

B-50 起上传落 ``agents/<key>/uploads/``(老视图),B-262 起对话视图落
``agents/<key>/threads/<会话>/work/uploads/``;此前作业按 ``{用户根}/uploads/<名字>`` 删,
一次都没删到,而旧测试把文件放在旧位置,给坏版本发了合格证。

这里的夹具一律用 control-plane 自己的 ``thread_workspace_path`` 摆文件 —— 上传端点写、
下载端点读都走它,所以文件就在真实位置上,不是测试自己猜的位置。
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest

from control_plane.api._workspace_shared import thread_workspace_path
from expert_work.persistence import InMemoryThreadMetaStore, InMemoryUserUploadStore
from expert_work.protocol import ThreadMeta
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

    assert await world.job()._sweep_uploads() == (1, 1)

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

    assert await world.job()._sweep_uploads() == (1, 1)

    assert not real.exists()
    assert agent_root_copy.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_a_missing_file_still_expires_the_row(world: _World) -> None:
    meta = await world.thread(layout="conversation")
    upload = await world.register(meta.thread_id)

    assert await world.job()._sweep_uploads() == (1, 0)

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

    assert await world.job()._sweep_uploads() == (1, 0)

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

    assert await world.job()._sweep_uploads() == (2, 1)

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

    assert await world.job(batch_size=1)._sweep_uploads() == (1, 1)

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

    assert await world.job()._sweep_uploads() == (1, 1)

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

    assert await world.job()._sweep_uploads() == (1, 1)

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

    assert await world.job()._sweep_uploads() == (1, 0)

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

    assert await world.job()._sweep_uploads() == (1, int(removed))

    assert real.exists() is not removed


# ------------------------------------------------------------ thread gone


@pytest.mark.asyncio
async def test_a_row_whose_thread_is_gone_expires_without_touching_files(world: _World) -> None:
    meta = await world.thread(layout="agent_root")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)
    del world.threads._rows[meta.thread_id]

    assert await world.job()._sweep_uploads() == (1, 0)

    assert real.exists()
    assert await world.deleted(upload)


@pytest.mark.asyncio
async def test_without_a_thread_store_nothing_is_touched(world: _World) -> None:
    """路径要按会话算;没接会话库就算不出来 —— 不删文件,行也不标(行是文件唯一的索引)。"""
    meta = await world.thread(layout="agent_root")
    real = world.place(meta)
    upload = await world.register(meta.thread_id)

    assert await world.job(thread_store=None)._sweep_uploads() == (0, 0)

    assert real.exists()
    assert not await world.deleted(upload)


# ------------------------------------------------------------ symlinks


@pytest.mark.asyncio
@pytest.mark.parametrize("segment", ["work", "uploads", "leaf"])
async def test_a_symlink_anywhere_on_the_path_is_not_followed(
    world: _World, tmp_path_factory: pytest.TempPathFactory, segment: str
) -> None:
    """路径上任何一段是软链(对话 ``work/`` 是模型写得到的地方)→ 不跟、不删,行留着明天再试。"""
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

    assert await world.job()._sweep_uploads() == (0, 0)

    assert victim.read_bytes() == _BODY
    assert not await world.deleted(upload)


# ------------------------------------------------------------ parity


@pytest.mark.parametrize(
    ("layout", "agent"),
    [("conversation", "support-bot"), ("agent_root", "support-bot"), ("conversation", None)],
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
