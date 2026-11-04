"""B-271 —— 上传 90 天这一条规则的本体(:meth:`RetentionCleanupJob._sweep_uploads` 调它)。

每条到期的文档类行:按登记行那条会话**当前**的 agent 与布局算出真实路径
(:func:`~retention_cleanup_job.workspace_files.upload_relpath`),核对后删文件;行一律软删
(对用户的到期语义不变)。下面几种情况**不删文件**,行照样软删:

* 会话不在了 —— 算不出路径。对话视图的目录随会话 purge 走;**老视图的文件在 agent 共用的
  ``agents/<key>/uploads/`` 里,purge 不删它,这里也不删,留在那里**(无从判断还有没有别的
  会话在用)。
* 还有别的没到期、没删的行按它自己会话的布局解析到同一个文件(老视图同一 agent 的各对话共用
  ``uploads/``,同名同内容复用)。删之前再查一次,收窄与同内容重传插行之间的竞态。
* 文件与登记时不一致(大小;有 sha256 时还比内容)—— 上传后在会话里被改写过。没有 sha256 的
  老行,先按同一会话、同名、同大小、时间贴着登记行的 ``DOCUMENT_UPLOAD`` 审计行补 sha256
  (唯一一个值才用),找不到就只比大小。
* 路径形状不安全(某段是软链 / 不是目录、叶子不是普通文件)—— 永久性的,不删文件、记一条
  warning、计数,行照样软删:留着不标就会天天重试,按 ``created_at`` 排在最前把整批饿死。

暂时性 I/O 失败或单行查库出错:这一行跳过、明天再试,不影响同批其它行。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from expert_work.persistence.audit_log import AuditLogStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.persistence.workspace.layout import WORKSPACE_UPLOADS_DIR
from expert_work.protocol import ThreadMeta, UserUpload
from retention_cleanup_job.workspace_files import (
    UnsafeWorkspacePathError,
    open_upload_file,
    upload_relpath,
)

logger = logging.getLogger(__name__)

#: 审计行与登记行贴得多近才算「同一次上传」:审计在落盘之后、登记行插入之前写,所以在登记行
#: 之前;往后留一点是给应用与库两边时钟的差。
_AUDIT_BEFORE = timedelta(minutes=10)
_AUDIT_AFTER = timedelta(minutes=1)

_ThreadKey = tuple[UUID, UUID]


@dataclass
class UploadSweepTally:
    marked: int = 0
    files_removed: int = 0
    files_unsafe: int = 0


def _is_document(row: UserUpload) -> bool:
    # 图片类的 ``ref`` 是 ``expert_work://image/…`` 对象存储 URI,字节归 ``image_upload``
    # 自己的 90 天 pass(需要 OSS 凭据),这里不碰;行照样标 ``deleted_at``。
    return row.ref.startswith(f"{WORKSPACE_UPLOADS_DIR}/")


class UploadSweep:
    """一批到期行的处理。会话按批预取(每个租户一次 ``get_many``),审计补 sha256 也按批读。"""

    def __init__(
        self,
        *,
        uploads: UserUploadStore,
        threads: ThreadMetaStore,
        root: str,
        audit: AuditLogStore | None = None,
    ) -> None:
        self._uploads = uploads
        self._threads = threads
        self._root = root
        self._audit = audit
        self._metas: dict[_ThreadKey, ThreadMeta | None] = {}

    async def run(
        self, expired: list[UserUpload], *, cutoff: datetime, now: datetime
    ) -> UploadSweepTally:
        docs = [r for r in expired if _is_document(r)]
        await self._load_threads((r.tenant_id, r.thread_id) for r in docs)
        audited = await self._audited_sha256s([r for r in docs if r.sha256 is None])
        tally = UploadSweepTally()
        for row in expired:
            try:
                await self._expire(row, audited.get(row.id, row.sha256), cutoff, now, tally)
            except Exception:
                logger.exception("retention.upload_row_failed upload_id=%s", row.id)
        return tally

    async def _expire(
        self,
        row: UserUpload,
        sha256: str | None,
        cutoff: datetime,
        now: datetime,
        tally: UploadSweepTally,
    ) -> None:
        if _is_document(row):
            try:
                removed = await self._release_file(row, sha256, cutoff)
            except UnsafeWorkspacePathError:
                logger.warning("retention.upload_path_unsafe upload_id=%s", row.id)
                tally.files_unsafe += 1
                removed = False
            tally.files_removed += int(removed)
        if await self._uploads.soft_delete(upload_id=row.id, tenant_id=row.tenant_id, now=now):
            tally.marked += 1

    async def _release_file(self, row: UserUpload, sha256: str | None, cutoff: datetime) -> bool:
        meta = self._metas.get((row.tenant_id, row.thread_id))
        if meta is None:
            logger.info("retention.upload_thread_gone upload_id=%s", row.id)
            return False
        relpath = upload_relpath(row.ref, meta)
        if await self._still_referenced(row, relpath, cutoff):
            return False
        opened = await asyncio.to_thread(
            open_upload_file,
            self._root,
            row.tenant_id,
            row.user_id,
            relpath,
            size_bytes=row.size_bytes,
            sha256=sha256,
        )
        try:
            if opened.outcome != "present":
                if opened.outcome == "changed":
                    logger.info("retention.upload_file_changed_kept upload_id=%s", row.id)
                return False
            # 核对之后、删之前再查一次:同内容重传刚插的行要能留住文件。
            if await self._still_referenced(row, relpath, cutoff):
                return False
            outcome = await asyncio.to_thread(opened.unlink)
        finally:
            opened.close()
        if outcome == "changed":
            logger.info("retention.upload_file_replaced_kept upload_id=%s", row.id)
        return outcome == "removed"

    async def _still_referenced(self, row: UserUpload, relpath: str, cutoff: datetime) -> bool:
        """别的没到期、没删的登记行按**它自己**会话的布局解析到 ``relpath`` 吗。

        ``ref`` 恒是 ``uploads/<一段名字>``,不同 ``ref`` 拼不出同一个路径,所以候选只需同租户、
        同用户、同 ``ref`` 的行;同 ``ref`` 但落在别处(另一条是对话视图)的不算,会话已不在的
        候选解析不出路径,也不算。
        """
        candidates = [
            other
            for other in await self._uploads.list_active_by_ref(
                tenant_id=row.tenant_id, user_id=row.user_id, ref=row.ref
            )
            if other.id != row.id and other.created_at >= cutoff
        ]
        await self._load_threads((c.tenant_id, c.thread_id) for c in candidates)
        for other in candidates:
            meta = self._metas.get((other.tenant_id, other.thread_id))
            if meta is not None and upload_relpath(other.ref, meta) == relpath:
                logger.info("retention.upload_file_still_referenced upload_id=%s", row.id)
                return True
        return False

    async def _load_threads(self, keys: Iterable[_ThreadKey]) -> None:
        """没缓存过的会话按租户一次 ``get_many``;查不到的记 ``None``(会话不在)。"""
        by_tenant: dict[UUID, set[UUID]] = {}
        for tenant_id, thread_id in keys:
            if (tenant_id, thread_id) not in self._metas:
                by_tenant.setdefault(tenant_id, set()).add(thread_id)
        for tenant_id, thread_ids in by_tenant.items():
            found = await self._threads.get_many(thread_ids, tenant_id=tenant_id)
            for thread_id in thread_ids:
                self._metas[(tenant_id, thread_id)] = found.get(thread_id)

    async def _audited_sha256s(self, rows: list[UserUpload]) -> dict[UUID, str]:
        """没有 sha256 的老行 → 登记时 ``DOCUMENT_UPLOAD`` 审计行里的 sha256。

        同一会话(``details.thread_id``)、同名(``workspace_path == ref``)、同大小、``occurred_at``
        落在登记行 ``created_at`` 前 10 分钟到后 1 分钟之间;这些审计行里的 sha256 **只有一个值**
        才用(B-262 之前同名上传会覆盖,同一窗口里两份不同内容就判不清,退回只比大小)。每个租户
        一次读。审计行早于这一轮的审计删除 pass 读(见 ``run_once`` 的顺序)。
        """
        if self._audit is None or not rows:
            return {}
        by_tenant: dict[UUID, list[UserUpload]] = {}
        for row in rows:
            by_tenant.setdefault(row.tenant_id, []).append(row)
        out: dict[UUID, str] = {}
        for tenant_id, tenant_rows in by_tenant.items():
            entries = await self._audit.list_document_uploads_for_threads(
                tenant_id=tenant_id,
                thread_ids={r.thread_id for r in tenant_rows},
                since=min(r.created_at for r in tenant_rows) - _AUDIT_BEFORE,
                until=max(r.created_at for r in tenant_rows) + _AUDIT_AFTER,
            )
            for row in tenant_rows:
                shas = {
                    e.details.get("sha256")
                    for e in entries
                    if e.occurred_at is not None
                    and row.created_at - _AUDIT_BEFORE <= e.occurred_at
                    and e.occurred_at <= row.created_at + _AUDIT_AFTER
                    and e.details.get("thread_id") == str(row.thread_id)
                    and e.details.get("workspace_path") == row.ref
                    and e.details.get("file_size_bytes") == row.size_bytes
                }
                if len(shas) == 1 and isinstance(sha := next(iter(shas)), str):
                    out[row.id] = sha
        return out


__all__ = ["UploadSweep", "UploadSweepTally"]
