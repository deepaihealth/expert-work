"""B-282 —— 闲置会话的工作目录到期清理(:meth:`RetentionCleanupJob._sweep_conversation_dirs` 调它)。

B-236 起每个会话有自己的 ``agents/<key>/threads/<会话>/``:``work/``(就是那段会话的
``/workspace``)、``PLAN.md`` 投影、开场复制标记 ``.seeded``。以前只有清除(会话 / 用户 /
智能体)删它;对外接口没有删除会话,所以对外的会话目录永远不清。这里按**闲置**清:

* 最后活动 = ``greatest(会话 created_at, 会话最后一次 run, 会话里没删的文档上传最后一次被用
  (``greatest(created_at, last_used_at)``))``,都只认同租户的行;持锁再查时另看这段会话到期线
  之后有没有成功的 ``DOCUMENT_UPLOAD`` 审计(B-281 之前的控制台上传与跨会话复制只有审计)。
  早于 ``now - 租户的 tenant_config.conversation_workspace_retention_days``(没设用平台默认 90)。
* 会话还有 ``pending`` / ``queued`` / ``running`` / ``paused`` 的 run 就不动 —— ``paused`` 是停在
  审批处、批了之后要接着用这个目录跑的 run。
* **再查与删持开场复制锁**(control-plane ``PgConversationSeedLock`` 的同一把 advisory lock,
  键 ``{tenant}:{会话}``):持锁再查一遍活动,删,删完再查一遍 —— 删的同时起了 run(起 run 不拿
  这把锁,但它的开场复制在锁后面等着)就记 ``raced``、不记清理时间。跨会话复制附件
  (``copy_document_into_thread``)不拿这把锁,它在建 run 之前落文件并写 ``DOCUMENT_UPLOAD``
  审计,持锁再查看得到那条审计;剩下的窗口是「文件已写、审计还没提交」的毫秒级。
* 删整棵 ``threads/<会话>/``(对话视图的会话):``work/``、``PLAN.md``、``.seeded``、
  ``.platform-migrated`` 一起走。会话之后再有 run 时没有标记,开场复制照常再做一遍
  (``conversation_seed``),迁移也幂等地再看一遍 —— 与一段新会话的第一轮一样。
* 再删这段会话每个 run 在 agent 根(没绑 agent 的是用户根)下的 ``.tool_results/<run>/``
  (B-262 P1 之前的 run、以及老视图会话的 run 写在那里);里面有下载要读的产物文件的 run 目录
  不删(``tool_results_skipped_artifact``)。老视图会话(``agent_root``)只删这一项,
  它的 ``threads/<会话>/`` 不在这一条的范围里。
* **库里的行一律不动**:会话、历史、产物登记都留着。清完在 ``thread_meta.workspace_cleaned_at``
  记一笔,之后不再列它;会话又有了新 run(最后活动晚于这一笔)才重新算闲置。

**产物保护。** B-235 之前登记、至今仍是最新版的产物版本没有副本,下载时读的是工作区里那个
文件。会话目录里有这种文件的,**整个会话这一轮不动**(计 ``skipped_artifact``),不是跳过那几个
文件删其余的:目录在沙箱写得到的树里,逐个文件留下要按名字在里面走一遍,而整段跳过永远不会删到
下载要读的东西;等那条产物按自己的保留期(B-63)到期、或登记了带副本的新版,会话就能清。

**怎么删。** 一律走 :func:`~expert_work.persistence.workspace.safe_remove.remove_entry`:从 NAS 根
一段一段 ``O_NOFOLLOW`` 打开,途中任何一段是符号链接就不删(计 ``skipped_unsafe``,照样记清理
时间 —— 那是模型自己弄的形状,每晚重试也不会变);删不掉的挪进隔离区(满 7 天由隔离区那一条删)。
每段会话单独 ``try``:一段失败(计 ``failed``,不记清理时间,明天再试)不影响别的;有失败这条
pass 记进 ``failed_passes``(进程以非 0 退出,让人去看)。

**每轮上限。** 一轮最多处理 ``batch_size`` 段会话;最早闲置的在前,有产物保护的排在最后(它们
这一轮反正不删,不能让它们占满一批把别的会话饿死)。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import Select, Text, and_, cast, exists, false, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence import WORKSPACE_AGENTS_DIR
from expert_work.persistence.audit_log import AuditLogStore
from expert_work.persistence.models import (
    AgentRunRow,
    ArtifactRow,
    ArtifactVersionRow,
    TenantConfigRow,
    ThreadMetaRow,
    UserUploadRow,
)
from expert_work.persistence.workspace.safe_remove import (
    QUARANTINED,
    REMOVED,
    SKIPPED_SYMLINK,
    UNLINKED,
    remove_entry,
)
from expert_work.protocol import FILE_RETENTION_MIN_DAYS
from expert_work.protocol.agent_key import sanitize_agent_key
from retention_cleanup_job.workspace_files import (
    DELETED_DIR,
    THREADS_DIR,
    TOOL_RESULTS_DIR,
)

logger = logging.getLogger(__name__)

#: 还没结束的 run:有一个就不动这段会话的目录。``paused`` = 停在审批处,批了要接着跑。
ACTIVE_RUN_STATUSES = ("pending", "queued", "running", "paused")

_GONE = (REMOVED, UNLINKED, QUARANTINED)

#: control-plane ``advisory_locks.CONVERSATION_SEED_LOCK_CLASSID``(开场复制锁)。作业不 import
#: control-plane(那会把整个服务拖进来),值抄过来,``test_conversation_dirs`` 比对两边一致。
CONVERSATION_SEED_LOCK_CLASSID = 8624
#: 持锁事务的超时(同 control-plane ``workspace_lock._LOCK_TXN_TIMEOUT_MS``)。
_LOCK_TXN_TIMEOUT_MS = 360_000


@dataclass(frozen=True)
class IdleConversation:
    tenant_id: UUID
    thread_id: UUID
    user_id: UUID
    agent_name: str | None
    layout: str
    #: 这段会话这一轮生效的保留天数(租户设置,没设用平台默认)。
    days: int
    #: 会话目录里有 B-235 之前登记、仍是最新版的产物文件(下载读工作区)。
    protected: bool


@dataclass
class ConversationDirTally:
    #: 删掉的会话目录(含挪进隔离区的)。
    removed: int = 0
    #: 删掉的 agent 根 / 用户根下 ``.tool_results/<run>/``。
    tool_results_removed: int = 0
    #: 持锁再查时发现有了新活动(run / 上传 / 上传审计)或还有没结束的 run:这一轮不动。
    skipped_active: int = 0
    #: 有产物保护:这一轮整个不动。
    skipped_artifact: int = 0
    #: 某个 run 的 ``.tool_results/<run>/`` 里有下载要读的产物文件:这个 run 目录不删。
    tool_results_skipped_artifact: int = 0
    #: 路径中途有符号链接:不删,照样记清理时间。
    skipped_unsafe: int = 0
    #: 用户已软删(``{tenant}/.deleted/{user}``):整棵树归 janitor 归档,这里不碰、不记。
    skipped_owner_deleted: int = 0
    #: 删的时候(持着开场复制锁)新起了 run:删完才发现,记日志、不记清理时间。
    raced: int = 0
    #: 删除出错(挪不进隔离区、I/O、查库):不记清理时间,明天再试。
    failed: int = 0

    @property
    def needs_attention(self) -> bool:
        """有会话删失败 —— 调用方把这条 pass 记成失败(进程非 0 退出),让人去看。"""
        return self.failed > 0


def _idle_query(now: datetime, default_days: int, limit: int) -> Select[tuple[object, ...]]:
    """闲置到期、没有未结束 run、清过之后有过新活动(或从没清过)的会话,有产物保护的排在最后。

    **活动** = ``greatest(会话 created_at, 会话最后一次 run, 会话里没删的文档上传最后一次被用)``
    (上传的「被用」= ``greatest(created_at, last_used_at)``:新传了附件但还没跑、或这份附件被别的
    会话引用 / 被下载,B-63 让它的登记行续命,文件也得留着)。都只认同租户的行;run 与上传各用一个
    只对候选会话聚合一次的 CTE(走 ``ix_agent_run_thread_id`` / ``ix_user_upload_tenant_thread``)。
    控制台上传在 B-281 之前没有登记行,只有 ``DOCUMENT_UPLOAD`` 审计 —— 那一项在持锁再查时看
    (:meth:`ConversationDirSweep._still_idle`),不在这条查询里扫审计表。

    候选先粗筛:按 ``created_at``(保留期最短 :data:`FILE_RETENTION_MIN_DAYS` 天,更新的不可能
    到期),并且「从没清过,或清过之后有过 run / 上传」—— 清完没再动过的会话不进候选。
    """
    earliest = min(default_days, FILE_RETENTION_MIN_DAYS)
    run_after_clean = exists().where(
        AgentRunRow.tenant_id == ThreadMetaRow.tenant_id,
        AgentRunRow.thread_id == ThreadMetaRow.thread_id,
        AgentRunRow.created_at > ThreadMetaRow.workspace_cleaned_at,
    )
    upload_after_clean = exists().where(
        UserUploadRow.tenant_id == ThreadMetaRow.tenant_id,
        UserUploadRow.thread_id == ThreadMetaRow.thread_id,
        UserUploadRow.deleted_at.is_(None),
        func.greatest(UserUploadRow.created_at, UserUploadRow.last_used_at)
        > ThreadMetaRow.workspace_cleaned_at,
    )
    cand = (
        select(
            ThreadMetaRow.thread_id,
            ThreadMetaRow.tenant_id,
            ThreadMetaRow.user_id,
            ThreadMetaRow.agent_name,
            ThreadMetaRow.workspace_layout,
            ThreadMetaRow.created_at,
            ThreadMetaRow.workspace_cleaned_at,
        )
        .where(
            ThreadMetaRow.user_id.is_not(None),
            ThreadMetaRow.created_at < now - timedelta(days=earliest),
            or_(
                ThreadMetaRow.workspace_cleaned_at.is_(None),
                run_after_clean,
                upload_after_clean,
            ),
        )
        .cte("conv_candidates")
    )
    runs = (
        select(
            AgentRunRow.tenant_id,
            AgentRunRow.thread_id,
            func.max(AgentRunRow.created_at).label("at"),
            func.bool_or(AgentRunRow.status.in_(ACTIVE_RUN_STATUSES)).label("active"),
        )
        .join(
            cand,
            and_(
                AgentRunRow.thread_id == cand.c.thread_id,
                AgentRunRow.tenant_id == cand.c.tenant_id,
            ),
        )
        .group_by(AgentRunRow.tenant_id, AgentRunRow.thread_id)
        .cte("conv_runs")
    )
    uploads = (
        select(
            UserUploadRow.tenant_id,
            UserUploadRow.thread_id,
            func.max(func.greatest(UserUploadRow.created_at, UserUploadRow.last_used_at)).label(
                "at"
            ),
        )
        .join(
            cand,
            and_(
                UserUploadRow.thread_id == cand.c.thread_id,
                UserUploadRow.tenant_id == cand.c.tenant_id,
            ),
        )
        .where(UserUploadRow.deleted_at.is_(None), UserUploadRow.kind == "document")
        .group_by(UserUploadRow.tenant_id, UserUploadRow.thread_id)
        .cte("conv_uploads")
    )
    last_activity = func.greatest(cand.c.created_at, runs.c.at, uploads.c.at)
    days = func.coalesce(TenantConfigRow.conversation_workspace_retention_days, default_days)
    # 产物路径是用户根相对的 ``agents/<key>/threads/<会话>/work/…``;会话 id 是 UUID,含
    # ``/threads/<会话>/`` 这一段就是这段会话目录里的文件(不用拼 agent key)。
    marker = func.concat("/", THREADS_DIR, "/", cast(cand.c.thread_id, Text), "/")
    protected = exists().where(
        ArtifactRow.tenant_id == cand.c.tenant_id,
        ArtifactRow.user_id == cand.c.user_id,
        ArtifactRow.deleted_at.is_(None),
        ArtifactVersionRow.artifact_id == ArtifactRow.id,
        ArtifactVersionRow.version == ArtifactRow.latest_version,
        ArtifactVersionRow.snapshot_key.is_(None),
        func.strpos(ArtifactVersionRow.path_in_workspace, marker) > 0,
    )
    return (
        select(
            cand.c.tenant_id,
            cand.c.thread_id,
            cand.c.user_id,
            cand.c.agent_name,
            cand.c.workspace_layout,
            days.label("days"),
            protected.label("protected"),
        )
        .select_from(cand)
        .outerjoin(
            runs,
            and_(runs.c.thread_id == cand.c.thread_id, runs.c.tenant_id == cand.c.tenant_id),
        )
        .outerjoin(
            uploads,
            and_(uploads.c.thread_id == cand.c.thread_id, uploads.c.tenant_id == cand.c.tenant_id),
        )
        .outerjoin(TenantConfigRow, TenantConfigRow.tenant_id == cand.c.tenant_id)
        .where(
            func.coalesce(runs.c.active, false()).is_(false()),
            last_activity + func.make_interval(0, 0, 0, days) < now,
            or_(
                cand.c.workspace_cleaned_at.is_(None),
                cand.c.workspace_cleaned_at < last_activity,
            ),
        )
        .order_by(protected.asc(), last_activity.asc(), cand.c.thread_id.asc())
        .limit(limit)
    )


async def list_idle_conversations(
    sf: async_sessionmaker[AsyncSession], *, now: datetime, default_days: int, limit: int
) -> list[IdleConversation]:
    """跨租户列出这一轮要看的闲置会话。Caller MUST be inside an RLS bypass scope."""
    async with sf() as session:
        rows = (await session.execute(_idle_query(now, default_days, limit))).all()
    return [
        IdleConversation(
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_id=user_id,
            agent_name=agent_name,
            layout=layout,
            days=int(days),
            protected=bool(protected),
        )
        for tenant_id, thread_id, user_id, agent_name, layout, days, protected in rows
    ]


@dataclass(frozen=True)
class _Now:
    """持锁时这段会话的样子。"""

    run_ids: list[UUID]
    active: bool
    #: run 与没删的文档上传里最晚的那次活动(审计另查)。
    last_activity: datetime | None


async def _activity_now(session: AsyncSession, conv: IdleConversation) -> _Now:
    runs = (
        await session.execute(
            select(AgentRunRow.id, AgentRunRow.status, AgentRunRow.created_at).where(
                AgentRunRow.tenant_id == conv.tenant_id, AgentRunRow.thread_id == conv.thread_id
            )
        )
    ).all()
    upload_at = (
        await session.execute(
            select(
                func.max(func.greatest(UserUploadRow.created_at, UserUploadRow.last_used_at))
            ).where(
                UserUploadRow.tenant_id == conv.tenant_id,
                UserUploadRow.thread_id == conv.thread_id,
                UserUploadRow.deleted_at.is_(None),
                UserUploadRow.kind == "document",
            )
        )
    ).scalar_one_or_none()
    stamps = [at for _id, _status, at in runs] + ([upload_at] if upload_at is not None else [])
    return _Now(
        run_ids=[run_id for run_id, _status, _at in runs],
        active=any(status in ACTIVE_RUN_STATUSES for _id, status, _at in runs),
        last_activity=max(stamps, default=None),
    )


async def _protected_runs(session: AsyncSession, conv: IdleConversation) -> set[str]:
    """下载要读工作区的产物文件(B-235 之前登记、仍是最新版、产物没删)落在哪些
    ``.tool_results/<run>/`` 里 —— 这些 run 目录不删。"""
    paths = (
        await session.execute(
            select(ArtifactVersionRow.path_in_workspace)
            .join(ArtifactRow, ArtifactVersionRow.artifact_id == ArtifactRow.id)
            .where(
                ArtifactRow.tenant_id == conv.tenant_id,
                ArtifactRow.user_id == conv.user_id,
                ArtifactRow.deleted_at.is_(None),
                ArtifactVersionRow.version == ArtifactRow.latest_version,
                ArtifactVersionRow.snapshot_key.is_(None),
                func.strpos(ArtifactVersionRow.path_in_workspace, f"{TOOL_RESULTS_DIR}/") > 0,
            )
        )
    ).scalars()
    out: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part == TOOL_RESULTS_DIR:
                out.add(parts[i + 1])
    return out


def _remove_files(
    root: str, conv: IdleConversation, agent_key: str, run_ids: list[UUID]
) -> tuple[str | None, list[str]]:
    """同步(``asyncio.to_thread``):删会话目录(对话视图)与各 run 的 ``.tool_results/<run>/``。"""
    base = [str(conv.tenant_id), str(conv.user_id)]
    if agent_key:
        base += [WORKSPACE_AGENTS_DIR, agent_key]
    thread_dir: str | None = None
    if conv.layout == "conversation" and agent_key:
        thread_dir = remove_entry(
            root,
            [*base, THREADS_DIR],
            str(conv.thread_id),
            tenant_id=conv.tenant_id,
            owner_id=conv.user_id,
        )
    tool_results = [
        remove_entry(
            root,
            [*base, TOOL_RESULTS_DIR],
            str(run_id),
            tenant_id=conv.tenant_id,
            owner_id=conv.user_id,
        )
        for run_id in run_ids
    ]
    return thread_dir, tool_results


def _owner_deleted(root: str, conv: IdleConversation) -> bool:
    return os.path.lexists(Path(root) / str(conv.tenant_id) / DELETED_DIR / str(conv.user_id))


class ConversationDirSweep:
    """一轮:列出闲置会话,逐段持开场复制锁再查、删、记清理时间。"""

    def __init__(
        self,
        *,
        sf: async_sessionmaker[AsyncSession],
        root: str,
        default_days: int,
        batch_size: int,
        audit: AuditLogStore | None = None,
    ) -> None:
        self._sf = sf
        self._root = root
        self._default_days = default_days
        self._batch_size = batch_size
        self._audit = audit
        #: 中途中止时调用方照样拿得到已经做了的部分。
        self.tally = ConversationDirTally()

    async def run(self, *, now: datetime) -> ConversationDirTally:
        idle = await list_idle_conversations(
            self._sf, now=now, default_days=self._default_days, limit=self._batch_size
        )
        for conv in idle:
            try:
                await self._one(conv, now)
            except Exception as exc:
                self.tally.failed += 1
                logger.warning(
                    "retention.conversation_dir_failed thread_id=%s error=%s",
                    conv.thread_id,
                    type(exc).__name__,
                    exc_info=True,
                )
        return self.tally

    async def _one(self, conv: IdleConversation, now: datetime) -> None:
        tally = self.tally
        if conv.protected:
            tally.skipped_artifact += 1
            logger.info("retention.conversation_dir_artifact_kept thread_id=%s", conv.thread_id)
            return
        if _owner_deleted(self._root, conv):
            # 整棵树归 janitor 归档;不记清理时间(用户被清除时会话行一起走)。
            tally.skipped_owner_deleted += 1
            return
        cutoff = now - timedelta(days=conv.days)
        # 与开场复制同一把锁(``{tenant}:{会话}``,classid 同 control-plane 的
        # ``CONVERSATION_SEED_LOCK_CLASSID``):持锁期间这段会话不会开场复制;再查、删、记在
        # 同一个事务里。
        async with self._sf() as session, session.begin():
            await session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
            )
            await session.execute(text(f"SET LOCAL statement_timeout = {_LOCK_TXN_TIMEOUT_MS}"))
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:classid, hashtext(:k))"),
                {
                    "classid": CONVERSATION_SEED_LOCK_CLASSID,
                    "k": f"{conv.tenant_id}:{conv.thread_id}",
                },
            )
            before = await _activity_now(session, conv)
            if not await self._still_idle(conv, before, cutoff, now):
                tally.skipped_active += 1
                return
            protected_runs = await _protected_runs(session, conv)
            run_ids = [r for r in before.run_ids if str(r) not in protected_runs]
            tally.tool_results_skipped_artifact += len(before.run_ids) - len(run_ids)
            agent_key = sanitize_agent_key(conv.agent_name) if conv.agent_name else ""
            thread_dir, tool_results = await asyncio.to_thread(
                _remove_files, self._root, conv, agent_key, run_ids
            )
            if SKIPPED_SYMLINK in [thread_dir, *tool_results]:
                tally.skipped_unsafe += 1
                logger.warning("retention.conversation_dir_unsafe thread_id=%s", conv.thread_id)
            tally.removed += int(thread_dir in _GONE)
            tally.tool_results_removed += sum(1 for o in tool_results if o in _GONE)
            after = await _activity_now(session, conv)
            if after.run_ids != before.run_ids or after.last_activity != before.last_activity:
                # 起 run 不拿这把锁:删的同时它可能刚建好行。它的开场复制在锁后面等着,会重新准备
                # 工作目录;不记清理时间,等它再闲置。
                tally.raced += 1
                logger.warning("retention.conversation_dir_raced thread_id=%s", conv.thread_id)
                return
            await session.execute(
                update(ThreadMetaRow)
                .where(
                    ThreadMetaRow.tenant_id == conv.tenant_id,
                    ThreadMetaRow.thread_id == conv.thread_id,
                )
                # ``updated_at`` 原样写回:这一笔不是会话本身的变化,会话列表按它排序。
                .values(workspace_cleaned_at=now, updated_at=ThreadMetaRow.updated_at)
            )

    async def _still_idle(
        self, conv: IdleConversation, current: _Now, cutoff: datetime, now: datetime
    ) -> bool:
        """持锁再查:没有没结束的 run、run / 上传都早于到期线、到期线之后没有成功的上传审计。"""
        if current.active or (
            current.last_activity is not None and current.last_activity >= cutoff
        ):
            return False
        if self._audit is None:
            return True
        audited = await self._audit.list_document_uploads_for_threads(
            tenant_id=conv.tenant_id, thread_ids=[conv.thread_id], since=cutoff, until=now
        )
        return not audited


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ConversationDirSweep",
    "ConversationDirTally",
    "IdleConversation",
    "list_idle_conversations",
]
