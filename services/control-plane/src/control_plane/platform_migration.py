"""B-262 P3 —— 老对话在下一次新 run 开始前自动迁到「每个对话自己的平台目录」。

B-262 P1 起对话视图里 ``uploads/`` / ``inputs/`` / ``.tool_results/`` 是本对话
``threads/<conv>/work/`` 下的普通子目录,agent 根下的同名目录在 ``/agent`` 里被盖住。两种
对话的平台文件还在 agent 根:

* (a) ``thread_meta.workspace_layout = 'agent_root'`` 的老对话(B-236 之前开的);
* (b) 已是对话视图、但在 P1 之前开的(上传与工具结果当时 bind 的是 agent 根)。

**在哪里做。** 每条起**新** run 的入口(``api/runs._start_run``:控制台 / 对外 run、编辑重发、
重新生成、排队轮到后执行;``run_queue_worker._execute``:queue 模式)在把布局写进
``configurable`` **之前**调 :func:`prepare_run_workspace`,用它返回的布局。这样迁移的那一轮
从头到尾就跑在新视图里:``conversation_seed`` 节点照常做开场复制(情形 a 翻完布局之后
它第一次看到对话视图),``ToolContext`` 是对话视图,一次性说明也在这一轮。不在图入口节点里
做:``configurable`` 在图开跑之前就定了,节点里翻布局,这一轮的工具仍是老视图。审批续跑与
孤儿复活接着跑已有的 run,不迁(中途换视图,前半程写的文件后半程就找不到);触发器每次都新建
对话,没有可迁的。

**做什么**(review 3:要查库的都在拿锁之前查完;持锁只做「再看一次标记 + 复制」;每个进程同时
迁移的对话有上限,等不到名额这一轮不迁):

1. 本对话自己的上传(``user_upload`` 登记行 + 本会话的 ``DOCUMENT_UPLOAD`` 审计行;控制台
   上传只有审计)从 ``agents/<k>/uploads/`` 复制进 ``work/uploads/``,按审计的 sha256 核对;
   对不上(P0 之前被别的客户同名覆盖)或没有 sha256(核对不了)不复制、也不挡迁移;读不了
   (I/O 错)算失败。上传不计入字节上限 —— 它们是这个对话自己的东西,不能因为大就留在 agent 根;
2. 本对话 run 的 ``.tool_results/<run>/`` 复制进 ``work/.tool_results/``(run 取自 ``agent_run``
   行;委派子代没有行,另取检查点 ``viewed_figures`` 里的 run,在别的会话有行的不算),超过
   字节上限的跳过;
3. 情形 a:放锁之后比较再交换把布局翻成 ``conversation``(只有库里还是 ``agent_root`` 才翻,
   并发的另一份翻不翻都一样);
4. 写标记 ``threads/<conv>/.platform-migrated``。

情形 a 只看库里的布局、不看标记(模型写得到标记,不能让它把对话钉在老视图);标记只决定情形 b
要不要再复制一遍。先翻后写标记:两步之间崩了,下一次 run 看到「对话视图、没有标记」,按情形 b
再跑一遍(复制从不覆盖,幂等)。复制失败:不翻、不写标记,这一轮照老布局跑,下一次再试。
**任何失败都不让 run 失败。**
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from expert_work.common.observability import expert_work_counter
from expert_work.persistence import WORKSPACE_UPLOADS_DIR
from expert_work.persistence.audit_log import AuditLogStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.protocol import ThreadMeta
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools.figure_lookup import figure_run_ids
from orchestrator.tools.locks import KeyedLock
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.platform_migration import (
    MAX_MIGRATION_BYTES,
    ExpectedUpload,
    MigrationCopyReport,
    copy_platform_files,
    platform_migrated,
    write_migrated_marker,
)
from orchestrator.tools.registry import WorkspaceLayout

logger = logging.getLogger(__name__)

platform_migration_total = expert_work_counter(
    "expert_work_platform_migration_total",
    "B-262 P3 per-conversation platform-file migrations at run start, by outcome "
    "(migrated = old layout copied and flipped; copied_only = conversation layout, files "
    "copied; skipped = nothing to do; failed = no marker, retried next run).",
    ("outcome",),
)
platform_migration_bytes_total = expert_work_counter(
    "expert_work_platform_migration_bytes_total",
    "B-262 P3 bytes copied into conversation directories by the platform-file migration.",
)
platform_migration_files_total = expert_work_counter(
    "expert_work_platform_migration_files_total",
    "B-262 P3 files copied into conversation directories by the platform-file migration.",
)

#: 本对话建立之后的审计才可能是它的;往前留一点钟差。
_AUDIT_SLACK = timedelta(minutes=5)
#: run 被取消后最多等复制线程这么久再放锁(秒)。复制幂等、先写临时再链接,不等也不会留半个
#: 文件;等是为了锁放掉之后没有线程还在写。
CANCEL_DRAIN_S = 120.0
#: 每个进程同时迁移的对话数上限(review 3:迁移持着一个 advisory-lock 事务、占一个复制线程)。
MAX_CONCURRENT_MIGRATIONS = 4
#: 拿名额最多等这么久(秒);拿不到这一轮不迁,老视图照跑,下一轮再试。
SLOT_WAIT_S = 2.0


class _DeferredError(Exception):
    """这一轮不迁、也不算成功(没拿到名额 / 老对话没有审计来源):不翻、不写标记,下次再试。"""


@dataclass(frozen=True)
class PreparedWorkspace:
    """这一轮 run 用的布局,以及是不是这一轮把老对话翻成了对话视图(要发一次性说明)。"""

    workspace_layout: WorkspaceLayout
    migrated: bool = False


class RunReader(Protocol):
    """``RunStore`` 满足它:本会话的 ``agent_run`` 行、哪些 run id 有行。"""

    async def list_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> Sequence[Any]:
        """每一项带 ``run_id``。"""

    async def existing_ids(self, run_ids: Sequence[UUID], *, tenant_id: UUID) -> set[UUID]:
        """其中在本租户有 ``agent_run`` 行的。"""


class CheckpointReader(Protocol):
    async def aget_tuple(self, config: Any) -> Any:
        """LangGraph ``BaseCheckpointSaver.aget_tuple``。"""


@dataclass(frozen=True)
class _Plan:
    """拿锁之前读完的东西(review 3:持锁时不另开连接查库)。"""

    meta: ThreadMeta
    user_id: UUID
    agent_key: str
    uploads: list[ExpectedUpload]
    run_ids: list[UUID]

    @property
    def old_layout(self) -> bool:
        return self.meta.workspace_layout == "agent_root"


@dataclass
class PlatformMigration:
    """迁移要用的东西;``app.py`` 只在接了 NAS 工作区时建它(别的后端没有宿主侧的目录)。"""

    workspace_store: NasWorkspaceStore
    lock: KeyedLock
    threads: ThreadMetaStore
    uploads: UserUploadStore
    runs: RunReader | None
    audit_store: AuditLogStore | None = None
    checkpointer: CheckpointReader | None = None
    max_bytes: int = MAX_MIGRATION_BYTES
    max_concurrent: int = MAX_CONCURRENT_MIGRATIONS
    slot_wait_s: float = SLOT_WAIT_S
    _slots: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._slots = asyncio.Semaphore(self.max_concurrent)

    async def prepare(
        self, *, thread_id: UUID, tenant_id: UUID, workspace_layout: WorkspaceLayout
    ) -> PreparedWorkspace:
        """见模块 docstring。永不抛(取消除外)。

        顺序(review 3):不持锁读完会话 / 上传来源 / run → 拿进程内名额 → 持锁只做「再看一次
        标记 + 复制」→ 放锁 → 比较再交换翻布局 → 写标记。老对话(库里还是 ``agent_root``)
        不看标记:模型写得到标记,它不能让自己的对话一直停在老视图;标记只管情形 b。
        """
        unchanged = PreparedWorkspace(workspace_layout)
        flipped = False
        agent_key = ""
        try:
            plan = await self._plan(thread_id, tenant_id)
            if plan is None:
                platform_migration_total.labels(outcome="skipped").inc()
                return PreparedWorkspace(
                    (await self._layout(thread_id, tenant_id)) or workspace_layout
                )
            agent_key = plan.agent_key
            report = await self._copy_holding_a_slot(plan)
            if report is None:  # 情形 b,锁内看到标记:别的副本刚做完
                platform_migration_total.labels(outcome="skipped").inc()
                return PreparedWorkspace(plan.meta.workspace_layout)
            if plan.old_layout:
                flipped = await self.threads.promote_workspace_layout(
                    thread_id, tenant_id=tenant_id
                )
            await asyncio.to_thread(
                write_migrated_marker,
                self.workspace_store,
                tenant_id,
                plan.user_id,
                agent_key,
                thread_id,
                report,
            )
        except asyncio.CancelledError:
            raise
        except _DeferredError as exc:
            platform_migration_total.labels(outcome="skipped").inc()
            logger.info("platform_migration.deferred agent_key=%s reason=%s", agent_key, exc)
            return unchanged
        except Exception as exc:
            platform_migration_total.labels(outcome="failed").inc()
            logger.warning(
                "platform_migration.failed agent_key=%s flipped=%s error=%s",
                agent_key,
                flipped,
                type(exc).__name__,
            )
            # 翻成功、标记没写成:这一轮已经是对话视图(下一轮按情形 b 补标记)。
            return PreparedWorkspace("conversation", migrated=True) if flipped else unchanged
        outcome = "migrated" if flipped else "copied_only"
        platform_migration_total.labels(outcome=outcome).inc()
        platform_migration_bytes_total.inc(report.bytes_copied)
        platform_migration_files_total.inc(report.files_copied)
        logger.info(
            "platform_migration.done outcome=%s agent_key=%s files=%d bytes=%d "
            "uploads_copied=%d uploads_existing=%d uploads_missing=%d uploads_mismatched=%d "
            "uploads_unverifiable=%d runs_copied=%d runs_existing=%d runs_missing=%d "
            "runs_over_cap=%d skipped_entries=%d",
            outcome,
            agent_key,
            report.files_copied,
            report.bytes_copied,
            report.uploads_copied,
            report.uploads_existing,
            report.uploads_missing,
            report.uploads_mismatched,
            report.uploads_unverifiable,
            report.runs_copied,
            report.runs_existing,
            report.runs_missing,
            report.runs_over_cap,
            report.skipped_entries,
        )
        return PreparedWorkspace("conversation", migrated=flipped)

    async def _layout(self, thread_id: UUID, tenant_id: UUID) -> WorkspaceLayout | None:
        meta = await self.threads.get(thread_id, tenant_id=tenant_id)
        return meta.workspace_layout if meta is not None else None

    async def _plan(self, thread_id: UUID, tenant_id: UUID) -> _Plan | None:
        """不持锁读完要用的东西;没什么可做(没绑 agent / 情形 b 已有标记)回 ``None``。"""
        meta = await self.threads.get(thread_id, tenant_id=tenant_id)
        agent_key = sanitize_agent_key(meta.agent_name) if meta and meta.agent_name else ""
        if meta is None or meta.user_id is None or not agent_key:
            return None
        old_layout = meta.workspace_layout == "agent_root"
        if not old_layout and await self._marked(tenant_id, meta.user_id, agent_key, thread_id):
            return None
        if old_layout and self.audit_store is None:
            # 控制台上传只有审计行:没有审计来源就会悄悄落下它们,老对话不翻。
            raise _DeferredError("no audit store")
        return _Plan(
            meta=meta,
            user_id=meta.user_id,
            agent_key=agent_key,
            uploads=await self._expected_uploads(meta),
            run_ids=await self._run_ids(meta),
        )

    async def _copy_holding_a_slot(self, plan: _Plan) -> MigrationCopyReport | None:
        """拿名额(等不到就这一轮不迁)→ 持锁:情形 b 再看一次标记、复制。锁里不碰库。"""
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.slot_wait_s)
        except TimeoutError as exc:
            raise _DeferredError("busy") from exc
        try:
            meta = plan.meta
            async with self.lock.acquire(key=f"{meta.tenant_id}:{meta.thread_id}"):
                if not plan.old_layout and await self._marked(
                    meta.tenant_id, plan.user_id, plan.agent_key, meta.thread_id
                ):
                    return None
                return await self._copy(
                    meta, plan.user_id, plan.agent_key, plan.uploads, plan.run_ids
                )
        finally:
            self._slots.release()

    async def _marked(self, tenant_id: UUID, user_id: UUID, agent_key: str, conv: UUID) -> bool:
        return await asyncio.to_thread(
            platform_migrated, self.workspace_store, tenant_id, user_id, agent_key, conv
        )

    async def _expected_uploads(self, meta: ThreadMeta) -> list[ExpectedUpload]:
        """本对话上传过的名字 → 上传时的大小 / sha256(登记行 + 审计行合并,按出现先后)。"""
        sizes: dict[str, set[int]] = {}
        shas: dict[str, set[str]] = {}
        for row in await self.uploads.list_documents_for_thread(
            tenant_id=meta.tenant_id, thread_id=meta.thread_id
        ):
            name = _upload_name(row.ref)
            if name is not None:
                sizes.setdefault(name, set()).add(row.size_bytes)
                shas.setdefault(name, set())
        for details in await self._audited_uploads(meta):
            name = _upload_name(details.get("workspace_path"))
            if name is None:
                continue
            sizes.setdefault(name, set())
            shas.setdefault(name, set())
            if isinstance(details.get("file_size_bytes"), int):
                sizes[name].add(details["file_size_bytes"])
            if isinstance(details.get("sha256"), str):
                shas[name].add(details["sha256"])
        return [
            ExpectedUpload(name=name, sizes=frozenset(sizes[name]), sha256s=frozenset(shas[name]))
            for name in sizes
        ]

    async def _audited_uploads(self, meta: ThreadMeta) -> list[dict[str, Any]]:
        """本对话的 ``DOCUMENT_UPLOAD`` 审计行(控制台上传没有登记行,只有它)。按会话查,不翻
        整个租户(review 3)。"""
        if self.audit_store is None:
            return []
        since = meta.created_at - _AUDIT_SLACK if meta.created_at is not None else None
        entries = await self.audit_store.list_document_uploads_for_thread(
            tenant_id=meta.tenant_id, thread_id=meta.thread_id, since=since
        )
        return [dict(e.details) for e in entries if e.details]

    async def _run_ids(self, meta: ThreadMeta) -> list[UUID]:
        """本对话的 run:``agent_run`` 行;委派子代没有行,另取检查点 ``viewed_figures`` 的 run
        (子代渲染、本对话看过的页)—— 其中在**别的**会话有行的丢掉(review 3:那是别的对话的
        run)。子代只写了超长结果、没渲过页的,找不回来(见 spec)。"""
        if self.runs is None:
            return []
        runs = [
            r.run_id
            for r in await self.runs.list_by_thread(
                thread_id=meta.thread_id, tenant_id=meta.tenant_id
            )
        ]
        if self.checkpointer is not None:
            config = {"configurable": {"thread_id": str(meta.thread_id), "checkpoint_ns": ""}}
            tup = await self.checkpointer.aget_tuple(config)
            if tup is not None:
                values = tup.checkpoint.get("channel_values") or {}
                refs = [r for r in values.get("viewed_figures") or () if isinstance(r, str)]
                extra = sorted(figure_run_ids(refs) - set(runs), key=str)
                if extra:
                    foreign = await self.runs.existing_ids(extra, tenant_id=meta.tenant_id)
                    runs += [r for r in extra if r not in foreign]
        return list(dict.fromkeys(runs))

    async def _copy(
        self,
        meta: ThreadMeta,
        user_id: UUID,
        agent_key: str,
        uploads: list[ExpectedUpload],
        run_ids: list[UUID],
    ) -> MigrationCopyReport:
        """在线程里复制;本协程被取消时等线程停下再往外抛(锁一直持到线程不再写)。"""
        work = asyncio.ensure_future(
            asyncio.to_thread(
                copy_platform_files,
                self.workspace_store,
                meta.tenant_id,
                user_id,
                agent_key,
                meta.thread_id,
                uploads=uploads,
                run_ids=run_ids,
                max_bytes=self.max_bytes,
            )
        )
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(work), timeout=CANCEL_DRAIN_S)
            raise


def _upload_name(ref: object) -> str | None:
    """``uploads/<名字>`` → ``<名字>``;别的形状不认。"""
    prefix = f"{WORKSPACE_UPLOADS_DIR}/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return None
    name = ref[len(prefix) :]
    return name or None


async def prepare_run_workspace(
    migration: PlatformMigration | None,
    *,
    thread_id: UUID,
    tenant_id: UUID,
    workspace_layout: WorkspaceLayout,
) -> PreparedWorkspace:
    """起**新** run 的入口在把布局写进 ``configurable`` 之前调。没接迁移(非 NAS 部署 / 单测)
    就原样用会话的布局。"""
    if migration is None:
        return PreparedWorkspace(workspace_layout)
    return await migration.prepare(
        thread_id=thread_id, tenant_id=tenant_id, workspace_layout=workspace_layout
    )
