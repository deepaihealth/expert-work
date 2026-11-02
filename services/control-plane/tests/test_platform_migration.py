"""B-262 P3 —— 老对话在下一次新 run 开始前自动迁移(控制面那一半)。

两种对话要迁:(a) ``workspace_layout = 'agent_root'`` 的老对话 —— 复制本对话自己的上传与工具
结果、把布局翻成 ``conversation``(这一轮就跑在新视图里,并给模型一句一次性说明);(b) 已是
对话视图、但平台文件还在 agent 根的(P1 之前开的)—— 只复制。做完写标记,之后的 run 什么都
不做。失败不让 run 失败:不写标记、不翻布局,下一次再试。

真文件系统(:class:`NasWorkspaceStore`)+ 内存仓库。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY

from control_plane.platform_migration import PlatformMigration, prepare_run_workspace
from expert_work.persistence import (
    InMemoryThreadMetaStore,
    InMemoryUserUploadStore,
)
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditEntry, AuditResult
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools.locks import ProcessKeyedLock
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import workspace_figure_ref

_AGENT = "support-bot"
_KEY = sanitize_agent_key(_AGENT)


@dataclass
class _Run:
    run_id: UUID


@dataclass
class _Runs:
    """``RunStore`` 的两个读法:本会话的 ``agent_run`` 行、哪些 run id 有行。"""

    by_thread: dict[UUID, list[UUID]] = field(default_factory=dict)

    async def list_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> list[_Run]:
        del tenant_id
        return [_Run(r) for r in self.by_thread.get(thread_id, [])]

    async def existing_ids(self, run_ids: Any, *, tenant_id: UUID) -> set[UUID]:
        del tenant_id
        known = {r for runs in self.by_thread.values() for r in runs}
        return {r for r in run_ids if r in known}


@dataclass
class _Tuple:
    checkpoint: dict[str, Any]


@dataclass
class _Checkpointer:
    viewed: dict[str, list[str]] = field(default_factory=dict)

    async def aget_tuple(self, config: Any) -> _Tuple | None:
        thread = config["configurable"]["thread_id"]
        if thread not in self.viewed:
            return None
        return _Tuple(checkpoint={"channel_values": {"viewed_figures": self.viewed[thread]}})


@dataclass
class _World:
    root: Path
    tenant: UUID
    user: UUID
    threads: InMemoryThreadMetaStore
    uploads: InMemoryUserUploadStore
    audit: InMemoryAuditLogStore
    runs: _Runs
    checkpointer: _Checkpointer
    migration: PlatformMigration

    def agent(self) -> Path:
        return self.root / str(self.tenant) / str(self.user) / "agents" / _KEY

    def conv(self, thread: UUID) -> Path:
        return self.agent() / "threads" / str(thread)

    def work(self, thread: UUID) -> Path:
        return self.conv(thread) / "work"

    def marker(self, thread: UUID) -> Path:
        return self.conv(thread) / ".platform-migrated"

    def put(self, rel: str, data: bytes) -> None:
        path = self.agent() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def thread(self, *, layout: str = "agent_root", agent: str | None = _AGENT) -> UUID:
        tid = uuid4()
        meta = await self.threads.create(
            thread_id=tid,
            tenant_id=self.tenant,
            created_by="u",
            user_id=self.user,
            agent_name=agent,
        )
        if layout != meta.workspace_layout:
            self.threads._rows[tid] = meta.model_copy(update={"workspace_layout": layout})
        return tid

    async def upload(self, thread: UUID, name: str, data: bytes) -> None:
        """对外上传:登记行 + 审计行(两条路都写)。"""
        await self.upload_row(thread, name, data)
        await self.audit_row(thread, name, data)

    async def upload_row(self, thread: UUID, name: str, data: bytes) -> None:
        await self.uploads.insert(
            upload_id=uuid4(),
            tenant_id=self.tenant,
            user_id=self.user,
            thread_id=thread,
            kind="document",
            ref=f"uploads/{name}",
            mime_type="application/pdf",
            size_bytes=len(data),
            filename=name,
        )

    async def audit_row(self, thread: UUID, name: str, data: bytes) -> None:
        """控制台上传:没有登记行,只有一条 ``DOCUMENT_UPLOAD`` 审计。"""
        await self.audit.append(
            AuditEntry(
                tenant_id=self.tenant,
                actor_type="user",
                actor_id="u",
                action=AuditAction.DOCUMENT_UPLOAD,
                resource_type="user_workspace",
                resource_id=f"uploads/{name}",
                result=AuditResult.SUCCESS,
                details={
                    "thread_id": str(thread),
                    "workspace_path": f"uploads/{name}",
                    "file_size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "reused_existing": False,
                },
            )
        )

    async def prepare(self, thread: UUID, layout: str | None = None) -> Any:
        meta = await self.threads.get(thread, tenant_id=self.tenant)
        assert meta is not None
        return await prepare_run_workspace(
            self.migration,
            thread_id=thread,
            tenant_id=self.tenant,
            workspace_layout=layout or meta.workspace_layout,
        )

    async def layout(self, thread: UUID) -> str:
        meta = await self.threads.get(thread, tenant_id=self.tenant)
        assert meta is not None
        return meta.workspace_layout


@pytest.fixture
def world(tmp_path: Path) -> _World:
    threads, uploads, audit = (
        InMemoryThreadMetaStore(),
        InMemoryUserUploadStore(),
        InMemoryAuditLogStore(),
    )
    runs, checkpointer = _Runs(), _Checkpointer()
    migration = PlatformMigration(
        workspace_store=NasWorkspaceStore(root=str(tmp_path)),
        lock=ProcessKeyedLock(),
        threads=threads,
        uploads=uploads,
        runs=runs,
        audit_store=audit,
        checkpointer=checkpointer,
    )
    return _World(
        tmp_path, uuid4(), uuid4(), threads, uploads, audit, runs, checkpointer, migration
    )


def _count(outcome: str) -> float:
    return (
        REGISTRY.get_sample_value("expert_work_platform_migration_total", {"outcome": outcome})
        or 0.0
    )


async def test_an_old_conversation_is_migrated_and_flipped(world: _World) -> None:
    """(a) 老视图:本对话的上传(登记行 + 只有审计的控制台上传)与它 run 的工具结果复制进
    ``work/``,布局翻成 ``conversation``,这一轮就按新视图跑、要发一次性说明。"""
    tid = await world.thread()
    run = uuid4()
    world.runs.by_thread[tid] = [run]
    await world.upload(tid, "a.pdf", b"AAAA")
    await world.audit_row(tid, "b.pdf", b"BBBB")
    world.put("uploads/a.pdf", b"AAAA")
    world.put("uploads/b.pdf", b"BBBB")
    world.put(f".tool_results/{run}/c1-bash.txt", b"overflow")
    before = _count("migrated")

    prepared = await world.prepare(tid)

    assert (prepared.workspace_layout, prepared.migrated) == ("conversation", True)
    assert await world.layout(tid) == "conversation"
    assert (world.work(tid) / "uploads" / "a.pdf").read_bytes() == b"AAAA"
    assert (world.work(tid) / "uploads" / "b.pdf").read_bytes() == b"BBBB"
    assert (world.work(tid) / ".tool_results" / str(run) / "c1-bash.txt").exists()
    assert world.marker(tid).is_file()
    assert _count("migrated") == before + 1


async def test_a_pre_p1_conversation_view_is_only_copied(world: _World) -> None:
    """(b) 已是对话视图、平台文件还在 agent 根:只复制,不发说明。"""
    tid = await world.thread(layout="conversation")
    await world.upload(tid, "a.pdf", b"AAAA")
    world.put("uploads/a.pdf", b"AAAA")
    before = _count("copied_only")

    prepared = await world.prepare(tid)

    assert (prepared.workspace_layout, prepared.migrated) == ("conversation", False)
    assert (world.work(tid) / "uploads" / "a.pdf").read_bytes() == b"AAAA"
    assert world.marker(tid).is_file()
    assert _count("copied_only") == before + 1


async def test_a_new_conversation_only_gets_the_marker(world: _World) -> None:
    tid = await world.thread(layout="conversation")

    prepared = await world.prepare(tid)

    assert (prepared.workspace_layout, prepared.migrated) == ("conversation", False)
    assert world.marker(tid).is_file()
    assert not [p for p in world.work(tid).rglob("*") if p.is_file()]


async def test_an_unbound_conversation_is_never_migrated(world: _World) -> None:
    tid = await world.thread(agent=None)
    before = _count("skipped")

    prepared = await world.prepare(tid)

    assert (prepared.workspace_layout, prepared.migrated) == ("agent_root", False)
    assert await world.layout(tid) == "agent_root"
    assert not (world.root / str(world.tenant)).exists()
    assert _count("skipped") == before + 1


async def test_the_marker_makes_it_happen_once(world: _World) -> None:
    tid = await world.thread()
    await world.upload(tid, "a.pdf", b"AAAA")
    world.put("uploads/a.pdf", b"AAAA")
    assert (await world.prepare(tid)).migrated is True
    (world.work(tid) / "uploads" / "a.pdf").unlink()

    again = await world.prepare(tid)

    assert (again.workspace_layout, again.migrated) == ("conversation", False)
    assert not (world.work(tid) / "uploads" / "a.pdf").exists()


async def test_a_layout_flipped_by_another_replica_is_not_announced_twice(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """比较再交换:另一个副本在我们读完之后先翻了 —— 这一轮跑新视图,但不再发说明。"""
    tid = await world.thread()
    real = world.threads.promote_workspace_layout

    async def raced(thread_id: UUID, *, tenant_id: UUID) -> bool:
        assert await real(thread_id, tenant_id=tenant_id) is True  # 「另一个副本」
        return await real(thread_id, tenant_id=tenant_id)

    monkeypatch.setattr(world.threads, "promote_workspace_layout", raced)

    prepared = await world.prepare(tid)

    assert (prepared.workspace_layout, prepared.migrated) == ("conversation", False)
    assert world.marker(tid).is_file()


async def test_a_failed_copy_leaves_no_marker_and_no_flip(world: _World) -> None:
    """复制失败(本对话的 ``work/uploads`` 被换成了符号链接):run 照跑老视图,不写标记、不翻,
    下一次再试。"""
    tid = await world.thread()
    await world.upload(tid, "a.pdf", b"AAAA")
    world.put("uploads/a.pdf", b"AAAA")
    world.work(tid).mkdir(parents=True)
    (world.work(tid) / "uploads").symlink_to(world.root)
    before = _count("failed")

    prepared = await world.prepare(tid)

    assert (prepared.workspace_layout, prepared.migrated) == ("agent_root", False)
    assert await world.layout(tid) == "agent_root"
    assert not world.marker(tid).exists()
    assert _count("failed") == before + 1

    (world.work(tid) / "uploads").unlink()
    assert (await world.prepare(tid)).migrated is True  # 下一次再试


async def test_another_customers_overwrite_is_not_copied(world: _World) -> None:
    """P0 之前同名上传会覆盖:agent 根里的那一份已是别的客户的。审计记下的 sha256 对不上,
    不复制。"""
    tid = await world.thread()
    await world.audit_row(tid, "report.pdf", b"mine")
    world.put("uploads/report.pdf", b"another customer")

    await world.prepare(tid)

    assert not (world.work(tid) / "uploads" / "report.pdf").exists()


async def test_child_run_pages_are_found_through_viewed_figures(world: _World) -> None:
    """委派子代的 run 没有 ``agent_run`` 行:它们渲染过、本对话看过的页,run 取自检查点的
    ``viewed_figures``。"""
    tid = await world.thread()
    child = uuid4()
    page = f".tool_results/{child}/figures/{'a' * 16}/{'b' * 16}/_u3/page-3.jpg"
    world.put(page, b"jpg")
    world.checkpointer.viewed[str(tid)] = [
        workspace_figure_ref(world.tenant, world.user, f"agents/{_KEY}/{page}")
    ]

    await world.prepare(tid)

    assert (world.work(tid) / page).exists()


async def test_the_byte_cap_applies_to_tool_results_only(world: _World) -> None:
    """review 3 —— 上传件不受上限(超了也照复制、照翻);上限只管工具结果。"""
    tid = await world.thread()
    first, second = uuid4(), uuid4()
    world.runs.by_thread[tid] = [first, second]
    await world.audit_row(tid, "big.pdf", b"X" * 20)
    world.put("uploads/big.pdf", b"X" * 20)
    world.put(f".tool_results/{first}/x.txt", b"A" * 6)
    world.put(f".tool_results/{second}/y.txt", b"B" * 6)
    small = replace(world.migration, max_bytes=10)

    prepared = await prepare_run_workspace(
        small, thread_id=tid, tenant_id=world.tenant, workspace_layout="agent_root"
    )

    assert prepared.migrated is True and world.marker(tid).exists()
    assert (world.work(tid) / "uploads" / "big.pdf").exists()
    assert (world.work(tid) / ".tool_results" / str(first)).exists()
    assert not (world.work(tid) / ".tool_results" / str(second)).exists()


async def test_a_size_only_upload_is_not_copied_but_does_not_block(world: _World) -> None:
    """review 3 —— 只有登记行(大小)、没有审计的 sha256:同样大小的别人的件也对得上,不复制;
    记成确认不了,不挡迁移。"""
    tid = await world.thread()
    await world.upload_row(tid, "notes.txt", b"mine: AAAA")
    world.put("uploads/notes.txt", b"theirs:BBB")

    prepared = await world.prepare(tid)

    assert prepared.migrated is True
    assert not (world.work(tid) / "uploads" / "notes.txt").exists()


async def test_a_run_of_another_thread_in_viewed_figures_is_not_copied(world: _World) -> None:
    """review 3 —— ``viewed_figures`` 里的 run 在别的会话有 ``agent_run`` 行:那是别人的,不复制
    (子代 run 没有行,照旧复制)。"""
    tid, other = await world.thread(), await world.thread()
    foreign = uuid4()
    world.runs.by_thread[other] = [foreign]
    page = f".tool_results/{foreign}/figures/{'a' * 16}/{'b' * 16}/_u3/page-3.jpg"
    world.put(page, b"jpg")
    world.put(f".tool_results/{foreign}/overflow-secret.txt", b"other customer's tool output")
    world.checkpointer.viewed[str(tid)] = [
        workspace_figure_ref(world.tenant, world.user, f"agents/{_KEY}/{page}")
    ]

    await world.prepare(tid)

    assert not (world.work(tid) / ".tool_results" / str(foreign)).exists()


async def test_an_unreadable_own_upload_fails_without_flip_or_marker(world: _World) -> None:
    """review 3 —— 本对话的上传读不动(不是「不在」、不是「对不上」):整次失败,不翻、不写标记,
    下次再试 —— 不能让它就这么滞留在 agent 根。"""
    tid = await world.thread()
    await world.upload(tid, "a.pdf", b"AAAA")
    world.put("uploads/a.pdf", b"AAAA")
    src = world.agent() / "uploads" / "a.pdf"
    src.chmod(0)
    try:
        prepared = await world.prepare(tid)
    finally:
        src.chmod(0o600)

    assert (prepared.workspace_layout, prepared.migrated) == ("agent_root", False)
    assert await world.layout(tid) == "agent_root"
    assert not world.marker(tid).exists()
    assert (await world.prepare(tid)).migrated is True


async def test_an_old_conversation_without_the_audit_store_is_not_flipped(world: _World) -> None:
    """review 3 —— 没有审计仓库就找不到控制台上传(它们只有审计行):老对话不翻,下次再试。"""
    tid = await world.thread()
    blind = replace(world.migration, audit_store=None)

    prepared = await prepare_run_workspace(
        blind, thread_id=tid, tenant_id=world.tenant, workspace_layout="agent_root"
    )

    assert (prepared.workspace_layout, prepared.migrated) == ("agent_root", False)
    assert await world.layout(tid) == "agent_root"
    assert not world.marker(tid).exists()


async def test_a_pre_created_marker_does_not_keep_an_old_conversation_old(world: _World) -> None:
    """review 3 —— 标记在模型写得到的 ``threads/<本对话>/`` 下:老对话按库里的布局判,标记只管
    情形 b。"""
    tid = await world.thread()
    await world.upload(tid, "a.pdf", b"AAAA")
    world.put("uploads/a.pdf", b"AAAA")
    world.conv(tid).mkdir(parents=True)
    world.marker(tid).write_text("planted")

    prepared = await world.prepare(tid)

    assert (prepared.workspace_layout, prepared.migrated) == ("conversation", True)
    assert (world.work(tid) / "uploads" / "a.pdf").exists()


@dataclass
class _WatchedLock:
    """记下持锁期间有没有发生别的库调用。"""

    held: bool = False
    inner: ProcessKeyedLock = field(default_factory=ProcessKeyedLock)

    @asynccontextmanager
    async def acquire(self, *, key: str) -> AsyncIterator[None]:
        async with self.inner.acquire(key=key):
            self.held = True
            try:
                yield
            finally:
                self.held = False


async def test_no_database_call_runs_while_the_lock_is_held(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 3 —— advisory lock 是连接池里的一个事务:持锁时不另开连接查库(那会让每个迁移占
    两条连接)。会话 / 登记行 / 审计 / run / 检查点在拿锁前读完,翻布局在放锁之后(比较再交换)。"""
    lock = _WatchedLock()
    migration = replace(world.migration, lock=lock)
    seen: list[str] = []

    def watch(obj: Any, name: str) -> None:
        real = getattr(obj, name)

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            if lock.held:
                seen.append(name)
            return await real(*args, **kwargs)

        monkeypatch.setattr(obj, name, wrapped)

    for obj, name in (
        (world.threads, "get"),
        (world.threads, "promote_workspace_layout"),
        (world.uploads, "list_documents_for_thread"),
        (world.audit, "list_document_uploads_for_thread"),
        (world.runs, "list_by_thread"),
        (world.runs, "existing_ids"),
        (world.checkpointer, "aget_tuple"),
    ):
        watch(obj, name)
    tid = await world.thread()
    await world.upload(tid, "a.pdf", b"AAAA")
    world.put("uploads/a.pdf", b"AAAA")

    prepared = await prepare_run_workspace(
        migration, thread_id=tid, tenant_id=world.tenant, workspace_layout="agent_root"
    )

    assert prepared.migrated is True
    assert seen == []


async def test_a_run_that_cannot_get_a_migration_slot_waits_for_the_next_run(
    world: _World,
) -> None:
    """review 3 —— 每个进程同时迁移的对话有上限;一小会儿拿不到名额就这一轮不迁(老视图照跑、
    不翻、不写标记),下一轮再试。"""
    busy = replace(world.migration, max_concurrent=1, slot_wait_s=0.05)
    tid = await world.thread()
    await busy._slots.acquire()  # 另一个对话正在迁
    try:
        prepared = await prepare_run_workspace(
            busy, thread_id=tid, tenant_id=world.tenant, workspace_layout="agent_root"
        )
    finally:
        busy._slots.release()

    assert (prepared.workspace_layout, prepared.migrated) == ("agent_root", False)
    assert await world.layout(tid) == "agent_root"
    assert not world.marker(tid).exists()
    assert (
        await prepare_run_workspace(
            busy, thread_id=tid, tenant_id=world.tenant, workspace_layout="agent_root"
        )
    ).migrated is True


async def test_no_migration_object_means_the_threads_layout(world: _World) -> None:
    tid = await world.thread()
    prepared = await prepare_run_workspace(
        None, thread_id=tid, tenant_id=world.tenant, workspace_layout="agent_root"
    )
    assert (prepared.workspace_layout, prepared.migrated) == ("agent_root", False)
