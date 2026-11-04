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
* 路径不可用(某段是软链 / 不是目录、叶子不是普通文件,或用户子树里除暂时性以外的任何
  ``OSError``:``chmod 000``、套接字……作业与沙箱同一个 uid,模型弄得出来)—— 永久性的,不删
  文件、记一条 warning、计数,行照样软删:留着不标就会天天重试,按 ``created_at`` 排在最前把
  整批饿死。

暂时性 I/O 失败或单行查库出错:这一行跳过、明天再试(``rows_failed``,每类一条 warning),
不影响同批其它行;过了到期日还连着失败 :data:`_GIVE_UP_AFTER` 的,不再等,行软删、文件不动
(``rows_abandoned``)—— 什么都不会无限重试。

「放弃」(路径不可用、过期太久)**先看完全批、后标记**:涉及的用户超过
:data:`_BREAKER_MAX_USERS` 个就一条都不标(``rows_held``)—— 那更像环境(uid / NAS 属主漂了)
或代码回归。有行被放弃或被拦下,调用方都把这个 pass 记成失败(进程退出码 3),让人去看。
配置的工作区根、``{tenant}`` / ``{user}`` 目录打不开同样是环境问题,整个 pass 失败。
"""

from __future__ import annotations

import asyncio
import errno
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from expert_work.persistence.audit_log import AuditLogStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.persistence.workspace.layout import WORKSPACE_UPLOADS_DIR
from expert_work.protocol import ThreadMeta, UserUpload
from retention_cleanup_job.workspace_files import (
    UnsafeWorkspacePathError,
    WorkspaceRootUnavailableError,
    open_upload_file,
    upload_relpath,
)

logger = logging.getLogger(__name__)

#: 审计行与登记行贴得多近才算「同一次上传」:审计在落盘之后、登记行插入之前写,所以在登记行
#: 之前;往后留一点是给应用与库两边时钟的差。
_AUDIT_BEFORE = timedelta(minutes=10)
_AUDIT_AFTER = timedelta(minutes=1)

#: 过了到期日还连着暂时性失败这么久,就不再等:行软删,文件不动(计数)。
_GIVE_UP_AFTER = timedelta(days=7)
#: 一次绑定多少个 id(``IN (...)`` 每个元素一个绑定参数,Postgres 上限 32767)。
_IN_CHUNK = 5000
#: 审计行里的 sha256 只认 64 位小写十六进制(上传端点 ``hexdigest()`` 的形状)。
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_ThreadKey = tuple[UUID, UUID]
#: 为什么放弃一行:路径不可用 / 过了到期日太久还在失败。
_GiveUp = Literal["unsafe", "abandoned"]

#: 熔断:一轮里要放弃的行涉及的用户超过这个数,就一条都不标、pass 记失败。模型弄坏路径
#: (软链、``chmod``、套接字)只碰得到**自己那个用户**的工作区(沙箱只挂这个用户的子目录),
#: 所以真实的这类情况每轮只牵涉一两个用户;一夜之间三个以上不相干的用户同时「坏」,更像
#: 作业的 uid / NAS 属主漂了(W2-BUG-1 出过)或代码回归 —— 那时放弃就是把整批文件变成孤儿。
#: 留 3 是给一两个巧合的余量。
_BREAKER_MAX_USERS = 3


@dataclass
class _FailureClass:
    count: int
    sample_upload_id: UUID
    sample: BaseException


def _failure_key(exc: BaseException) -> str:
    code = getattr(exc, "errno", None)
    name = errno.errorcode.get(code, str(code)) if isinstance(code, int) else "-"
    return f"{type(exc).__name__}/{name}"


def _note_failure(failures: dict[str, _FailureClass], row: UserUpload, exc: BaseException) -> None:
    key = _failure_key(exc)
    if key in failures:
        failures[key].count += 1
    else:
        failures[key] = _FailureClass(count=1, sample_upload_id=row.id, sample=exc)


def _log_failures(failures: dict[str, _FailureClass]) -> None:
    """暂时性失败每轮每类(异常类型 / errno)一条 warning + 计数,带一个样本的 traceback。"""
    for key, cls in failures.items():
        logger.warning(
            "retention.upload_rows_failed class=%s count=%d sample_upload_id=%s",
            key,
            cls.count,
            cls.sample_upload_id,
            exc_info=cls.sample,
        )


@dataclass
class UploadSweepTally:
    marked: int = 0
    files_removed: int = 0
    #: 路径不可用(软链 / 非普通文件 / 权限 / 套接字……)而没删文件的行;行已软删。
    files_unsafe: int = 0
    #: 暂时性失败、这一轮跳过的行(明天再试)。
    rows_failed: int = 0
    #: 过了到期日 :data:`_GIVE_UP_AFTER` 还在失败、不再等的行;行已软删、文件没动。
    rows_abandoned: int = 0
    #: 熔断(:data:`_BREAKER_MAX_USERS`)拦下、这一轮没标的「放弃」行。
    rows_held: int = 0

    @property
    def needs_attention(self) -> bool:
        """有行被放弃或被熔断拦下 —— 调用方把这个 pass 记成失败,让人去看。"""
        return self.rows_abandoned > 0 or self.rows_held > 0


def _chunks(ids: Iterable[UUID]) -> Iterator[list[UUID]]:
    ordered = sorted(set(ids), key=str)
    for start in range(0, len(ordered), _IN_CHUNK):
        yield ordered[start : start + _IN_CHUNK]


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
        try:
            audited = await self._audited_sha256s([r for r in docs if r.sha256 is None])
        except Exception:
            # 补 sha256 是锦上添花:读不到就退回只比大小,不让整个 pass 失败。
            logger.warning("retention.upload_audit_lookup_failed", exc_info=True)
            audited = {}
        tally = UploadSweepTally()
        giving_up: list[tuple[UserUpload, _GiveUp]] = []
        failures: dict[str, _FailureClass] = {}
        for row in expired:
            try:
                if await self._release(row, audited.get(row.id, row.sha256), cutoff, tally):
                    await self._mark(row, now, tally)
                else:
                    giving_up.append((row, "unsafe"))
            except WorkspaceRootUnavailableError:
                raise
            except Exception as exc:
                if row.created_at >= cutoff - _GIVE_UP_AFTER:
                    _note_failure(failures, row, exc)
                    tally.rows_failed += 1
                else:
                    logger.warning(
                        "retention.upload_row_abandoning upload_id=%s error=%s",
                        row.id,
                        type(exc).__name__,
                    )
                    giving_up.append((row, "abandoned"))
        _log_failures(failures)
        await self._give_up(giving_up, now, tally)
        return tally

    async def _release(
        self, row: UserUpload, sha256: str | None, cutoff: datetime, tally: UploadSweepTally
    ) -> bool:
        """文件那一半。``True`` → 照常到期(文件删了 / 不该删 / 本来不在);``False`` → 路径不可用,
        进「放弃」清单,等全批看完再由熔断决定标不标。"""
        if not _is_document(row):
            return True
        try:
            removed = await self._release_file(row, sha256, cutoff)
        except UnsafeWorkspacePathError:
            logger.warning("retention.upload_path_unusable upload_id=%s", row.id)
            return False
        tally.files_removed += int(removed)
        return True

    async def _mark(self, row: UserUpload, now: datetime, tally: UploadSweepTally) -> bool:
        if await self._uploads.soft_delete(upload_id=row.id, tenant_id=row.tenant_id, now=now):
            tally.marked += 1
            return True
        return False

    async def _give_up(
        self, giving_up: list[tuple[UserUpload, _GiveUp]], now: datetime, tally: UploadSweepTally
    ) -> None:
        """先看完、后标记:要放弃的行(路径不可用 / 过了到期日 7 天还在失败)涉及的用户超过
        :data:`_BREAKER_MAX_USERS` 个 → 这一轮一条都不标(``rows_held``,调用方记 pass 失败),
        因为那更像环境问题或代码回归,而不是哪个会话里的模型弄坏了自己的文件。"""
        users = {(row.tenant_id, row.user_id) for row, _ in giving_up}
        if len(users) > _BREAKER_MAX_USERS:
            tally.rows_held = len(giving_up)
            logger.error(
                "retention.upload_breaker_tripped rows=%d users=%d unsafe=%d abandoned=%d "
                "— not expiring them this run; looks environmental, not per-user",
                len(giving_up),
                len(users),
                sum(1 for _, why in giving_up if why == "unsafe"),
                sum(1 for _, why in giving_up if why == "abandoned"),
            )
            return
        for row, why in giving_up:
            try:
                if await self._mark(row, now, tally):
                    if why == "unsafe":
                        tally.files_unsafe += 1
                    else:
                        tally.rows_abandoned += 1
            except Exception as exc:
                logger.warning(
                    "retention.upload_row_failed upload_id=%s error=%s", row.id, type(exc).__name__
                )
                tally.rows_failed += 1

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
            for chunk in _chunks(thread_ids):
                found = await self._threads.get_many(chunk, tenant_id=tenant_id)
                for thread_id in chunk:
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
            entries = []
            for chunk in _chunks(r.thread_id for r in tenant_rows):
                entries += await self._audit.list_document_uploads_for_threads(
                    tenant_id=tenant_id,
                    thread_ids=chunk,
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
                if (
                    len(shas) == 1
                    and isinstance(sha := next(iter(shas)), str)
                    and _SHA256_RE.fullmatch(sha)
                ):
                    out[row.id] = sha
        return out


__all__ = ["UploadSweep", "UploadSweepTally"]
