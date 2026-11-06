"""B-282 —— 闲置会话的工作目录到期清理(:meth:`RetentionCleanupJob._sweep_conversation_dirs` 调它)。

B-236 起每个会话有自己的 ``agents/<key>/threads/<会话>/``:``work/``(就是那段会话的
``/workspace``)、``PLAN.md`` 投影、开场复制标记 ``.seeded``。以前只有清除(会话 / 用户 /
智能体)删它;对外接口没有删除会话,所以对外的会话目录永远不清。这里按**闲置**清:

* 最后活动 = ``greatest(会话 created_at, 会话最后一次 run, 会话里没删的文档上传最后一次被用
  (``greatest(created_at, last_used_at)``))``,都只认同租户的行;持锁再查时另看这段会话到期线
  之后(到现在为止)有没有成功的 ``DOCUMENT_UPLOAD`` 审计(B-281 之前的控制台上传与跨会话复制
  只有审计)。早于 ``now - 租户的 tenant_config.conversation_workspace_retention_days``(没设用
  平台默认 90)。
* **还有没删的文档上传登记行就不动**:``upl_`` 下载读的是这段会话 ``work/uploads/`` 里的文件,
  租户的上传保留期可以比会话的长(比如 365 对 90),目录先清了登记行却还活着,就是 404。上传
  自己到期归 B-63;登记行都到期(标了 ``deleted_at``)之后才轮到这里。
* 会话还有 ``pending`` / ``queued`` / ``running`` / ``paused`` 的 run 就不动 —— ``paused`` 是停在
  审批处、批了之后要接着用这个目录跑的 run。
* **持开场复制锁再查,然后改名挪走,不在锁里删**(control-plane ``PgConversationSeedLock`` 的同一把
  advisory lock,键 ``{tenant}:{会话}``)。上传落盘(``_land_and_account``,含跨会话复制附件
  ``copy_document_into_thread``)拿这把锁的**共享**形式:持锁期间没有落盘在进行,之前的落盘都已经
  有登记行 / 审计,再查看得到。再查通过就把 ``threads/<会话>/`` 与各 run 的 ``.tool_results/<run>/``
  一次 ``rename`` 挪进隔离区 ``{tenant}/.purged-agents/{user}/``(沙箱挂不到),再查一遍,记清理
  时间,提交放锁。之后按原路径写的东西落进新目录,不会被删;开场复制标记 ``.seeded`` 在挪走的那
  棵树里,下一个 run 照常重新复制。挪走的树满 7 天由隔离区那一条(``sweep_quarantine``)删,在
  那之前可以手工挪回。
* 起 run 不拿这把锁:再查之后、改名之前新建的 run(开场复制可能已经看到旧标记)在第二次查时看得
  到 —— 挪回原处(原名已被新目录占了就留在隔离区),记 ``raced``、不记清理时间。
* 老视图会话(``agent_root``)只挪各 run 的 ``.tool_results/<run>/``(B-262 P1 之前的 run 写在
  agent 根,没绑 agent 的在用户根);里面有下载要读的产物文件的 run 目录不动
  (``tool_results_skipped_artifact``)。
* **库里的行一律不动**:会话、历史、产物登记都留着。清完在 ``thread_meta.workspace_cleaned_at``
  记一笔,之后不再列它;会话又有了新 run / 上传(最后活动晚于这一笔)才重新算闲置。

**产物保护。** B-235 之前登记、至今仍是最新版的产物版本没有副本,下载时读的是工作区里那个
文件。会话目录里有这种文件的,**整个会话这一轮不动**(计 ``skipped_artifact``);等那条产物按
自己的保留期(B-63)到期、或登记了带副本的新版,会话就能清。

**路径安全。** :func:`~expert_work.persistence.workspace.safe_remove.quarantine_entry` 从 NAS 根一段
一段 ``O_NOFOLLOW`` 打开,途中任何一段是符号链接就不动(计 ``skipped_unsafe``,照样记清理时间
—— 那是模型自己弄的形状,每晚重试也不会变)。每段会话单独 ``try``:一段失败(计 ``failed``,
不记清理时间,明天再试)不影响别的;有失败这条 pass 记进 ``failed_passes``(进程以非 0 退出)。

**每轮上限与轮转。** 一轮最多处理 ``batch_size`` 段会话。有产物保护的排在最后;列到了却没清的
(再查发现有活动、删失败、产物保护……)记 ``thread_meta.workspace_clean_attempted_at``,下一轮
排在从没试过的后面,不让同一批清不了的会话每晚占满整批;已软删用户的会话在查询里就排除。
一批满了且一半以上没清,打 ``retention.conversation_dirs_mostly_skipped``。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    TenantUserRow,
    ThreadMetaRow,
    UserUploadRow,
)
from expert_work.persistence.workspace.safe_remove import (
    QUARANTINED,
    SKIPPED_SYMLINK,
    quarantine_entry,
    restore_quarantined,
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

#: 一轮里跳过(没清)的会话占整批的这么多以上、且这一批是满的,打一条 warning:多半是同一批会话
#: 每晚都被列出来又都清不了(审计里有上传、删失败……),别的会话排不进来。
_SKIPPED_WARN_RATIO = 0.5

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
    #: 挪进隔离区的会话目录(满 7 天由隔离区那一条删)。
    removed: int = 0
    #: 挪进隔离区的 agent 根 / 用户根下 ``.tool_results/<run>/``。
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
    #: 挪走的同时(持着开场复制锁)新起了 run:挪回原处,记日志、不记清理时间。
    raced: int = 0
    #: 删除出错(挪不进隔离区、I/O、查库):不记清理时间,明天再试。
    failed: int = 0
    #: 清了(记了清理时间)的会话数。
    cleaned: int = 0

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
    # M2 —— 还有没删的文档上传登记行:下载读的是这段会话的 ``work/uploads/``,目录一清,登记行
    # 还在(租户上传保留期可以比会话的长)却 404。上传自己到期归 B-63;到期之后才轮到这里。
    live_upload = exists().where(
        UserUploadRow.tenant_id == ThreadMetaRow.tenant_id,
        UserUploadRow.thread_id == ThreadMetaRow.thread_id,
        UserUploadRow.kind == "document",
        UserUploadRow.deleted_at.is_(None),
    )
    # M4 —— 已软删的用户:整棵树归 janitor 归档,这里永远不清、也不记,不能让它们占批。
    owner_gone = exists().where(
        TenantUserRow.tenant_id == ThreadMetaRow.tenant_id,
        TenantUserRow.id == ThreadMetaRow.user_id,
        TenantUserRow.deleted_at.is_not(None),
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
            ThreadMetaRow.workspace_clean_attempted_at,
        )
        .where(
            ThreadMetaRow.user_id.is_not(None),
            ThreadMetaRow.created_at < now - timedelta(days=earliest),
            ~live_upload,
            ~owner_gone,
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
        # 有产物保护的排最后;其余先排从没试过的,再按上次试的时间(M4:每晚都清不了的那几段
        # 轮到后面去,不把别的会话挤出这一批),同样的再按闲置先后。
        .order_by(
            protected.asc(),
            cand.c.workspace_clean_attempted_at.asc().nulls_first(),
            last_activity.asc(),
            cand.c.thread_id.asc(),
        )
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
    #: 还有没删的文档上传登记行(M2:有就不清)。
    live_upload: bool
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
    upload_at, live = (
        await session.execute(
            select(
                func.max(func.greatest(UserUploadRow.created_at, UserUploadRow.last_used_at)),
                func.count(),
            ).where(
                UserUploadRow.tenant_id == conv.tenant_id,
                UserUploadRow.thread_id == conv.thread_id,
                UserUploadRow.kind == "document",
                UserUploadRow.deleted_at.is_(None),
            )
        )
    ).one()
    stamps = [at for _id, _status, at in runs] + ([upload_at] if upload_at is not None else [])
    return _Now(
        run_ids=[run_id for run_id, _status, _at in runs],
        active=any(status in ACTIVE_RUN_STATUSES for _id, status, _at in runs),
        live_upload=live > 0,
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


@dataclass(frozen=True)
class _Parked:
    """挪进隔离区的一项:原来在 ``{parts…}/{name}``,现在叫 ``parked``。"""

    parts: list[str]
    name: str
    parked: str


def _park(
    root: str, conv: IdleConversation, agent_key: str, run_ids: list[UUID]
) -> tuple[list[_Parked], bool]:
    """同步(``asyncio.to_thread``):把会话目录(对话视图)与各 run 的 ``.tool_results/<run>/``
    **改名**挪进隔离区 ``{tenant}/.purged-agents/{user}/``(沙箱挂不到;满 7 天由隔离区那一条
    删)。返回挪走的项,与路径中途是否碰到符号链接。"""
    base = [str(conv.tenant_id), str(conv.user_id)]
    if agent_key:
        base += [WORKSPACE_AGENTS_DIR, agent_key]
    targets: list[tuple[list[str], str]] = []
    if conv.layout == "conversation" and agent_key:
        targets.append(([*base, THREADS_DIR], str(conv.thread_id)))
    targets += [([*base, TOOL_RESULTS_DIR], str(run_id)) for run_id in run_ids]
    parked: list[_Parked] = []
    unsafe = False
    for parts, name in targets:
        outcome, new_name = quarantine_entry(
            root, parts, name, tenant_id=conv.tenant_id, owner_id=conv.user_id
        )
        unsafe = unsafe or outcome == SKIPPED_SYMLINK
        if outcome == QUARANTINED and new_name is not None:
            parked.append(_Parked(parts, name, new_name))
    return parked, unsafe


def _unpark(root: str, conv: IdleConversation, parked: list[_Parked]) -> list[_Parked]:
    """同步:把 :func:`_park` 挪走的挪回原处;返回挪不回去的(原名又有了东西)。"""
    return [
        p
        for p in parked
        if not restore_quarantined(
            root, p.parts, p.name, tenant_id=conv.tenant_id, owner_id=conv.user_id, parked=p.parked
        )
    ]


def _owner_deleted(root: str, conv: IdleConversation) -> bool:
    return os.path.lexists(Path(root) / str(conv.tenant_id) / DELETED_DIR / str(conv.user_id))


class ConversationDirSweep:
    """一轮:列出闲置会话,逐段持开场复制锁再查、挪进隔离区、记清理时间。"""

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
                cleaned = await self._one(conv, now)
            except Exception as exc:
                cleaned = False
                self.tally.failed += 1
                logger.warning(
                    "retention.conversation_dir_failed thread_id=%s error=%s",
                    conv.thread_id,
                    type(exc).__name__,
                    exc_info=True,
                )
            if not cleaned:
                await self._note_attempt(conv, now)
        skipped = len(idle) - self.tally.cleaned
        if len(idle) >= self._batch_size and skipped >= _SKIPPED_WARN_RATIO * len(idle):
            logger.warning(
                "retention.conversation_dirs_mostly_skipped skipped=%d batch=%d", skipped, len(idle)
            )
        return self.tally

    async def _note_attempt(self, conv: IdleConversation, now: datetime) -> None:
        """M4 —— 这一轮列到了却没清:记一笔尝试时间,下一轮它排在没试过的后面。尽力而为。"""
        try:
            async with self._sf() as session, session.begin():
                await session.execute(
                    update(ThreadMetaRow)
                    .where(
                        ThreadMetaRow.tenant_id == conv.tenant_id,
                        ThreadMetaRow.thread_id == conv.thread_id,
                    )
                    .values(workspace_clean_attempted_at=now, updated_at=ThreadMetaRow.updated_at)
                )
        except Exception as exc:
            logger.warning(
                "retention.conversation_dir_attempt_not_noted thread_id=%s error=%s",
                conv.thread_id,
                type(exc).__name__,
            )

    async def _one(self, conv: IdleConversation, now: datetime) -> bool:
        """一段会话;清了(或本来就什么都没有)回 ``True``。"""
        tally = self.tally
        if conv.protected:
            tally.skipped_artifact += 1
            logger.info("retention.conversation_dir_artifact_kept thread_id=%s", conv.thread_id)
            return False
        if _owner_deleted(self._root, conv):
            # 整棵树归 janitor 归档;不记清理时间(用户被清除时会话行一起走)。
            tally.skipped_owner_deleted += 1
            return False
        cutoff = now - timedelta(days=conv.days)
        # 与开场复制同一把锁(``{tenant}:{会话}``,classid 同 control-plane 的
        # ``CONVERSATION_SEED_LOCK_CLASSID``)。上传落盘(``_land_and_account``,含跨会话复制附件)
        # 拿同一把锁的共享形式:持锁期间没有落盘在进行,之前落的都已经有登记行 / 审计。持锁只做
        # 再查 + 几次 ``rename`` + 记一笔,不在锁里删(大目录删很久,会超事务空闲超时把锁丢掉)。
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
            if not await self._still_idle(conv, before, cutoff):
                tally.skipped_active += 1
                return False
            protected_runs = await _protected_runs(session, conv)
            run_ids = [r for r in before.run_ids if str(r) not in protected_runs]
            tally.tool_results_skipped_artifact += len(before.run_ids) - len(run_ids)
            agent_key = sanitize_agent_key(conv.agent_name) if conv.agent_name else ""
            parked, unsafe = await asyncio.to_thread(_park, self._root, conv, agent_key, run_ids)
            if unsafe:
                tally.skipped_unsafe += 1
                logger.warning("retention.conversation_dir_unsafe thread_id=%s", conv.thread_id)
            after = await _activity_now(session, conv)
            if after != before:
                # 起 run 不拿这把锁:再查之后、改名之前它可能刚建好行,开场复制时看到的还是旧
                # 目录里的标记。挪回原处(原名已被新目录占了的留在隔离区,7 天内可手工挪回),
                # 不记清理时间,等它再闲置。
                stuck = await asyncio.to_thread(_unpark, self._root, conv, parked)
                tally.raced += 1
                logger.warning(
                    "retention.conversation_dir_raced thread_id=%s left_in_quarantine=%s",
                    conv.thread_id,
                    ",".join(p.parked for p in stuck) or "-",
                )
                return False
            await session.execute(
                update(ThreadMetaRow)
                .where(
                    ThreadMetaRow.tenant_id == conv.tenant_id,
                    ThreadMetaRow.thread_id == conv.thread_id,
                )
                # ``updated_at`` 原样写回:这一笔不是会话本身的变化,会话列表按它排序。
                .values(workspace_cleaned_at=now, updated_at=ThreadMetaRow.updated_at)
            )
        # 提交了(锁也放了)才算数;挪走的那几棵树留在隔离区,满 7 天删。
        thread_dir = str(conv.thread_id)
        tally.removed += sum(1 for p in parked if p.name == thread_dir)
        tally.tool_results_removed += sum(1 for p in parked if p.name != thread_dir)
        tally.cleaned += 1
        return True

    async def _still_idle(self, conv: IdleConversation, current: _Now, cutoff: datetime) -> bool:
        """持锁再查:没有没结束的 run、没有没删的文档上传登记行、run / 上传都早于到期线、到期线
        之后(到现在为止)没有成功的上传审计。"""
        if (
            current.active
            or current.live_upload
            or (current.last_activity is not None and current.last_activity >= cutoff)
        ):
            return False
        if self._audit is None:
            return True
        audited = await self._audit.list_document_uploads_for_threads(
            tenant_id=conv.tenant_id,
            thread_ids=[conv.thread_id],
            since=cutoff,
            until=datetime.now(UTC),
        )
        return not audited


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ConversationDirSweep",
    "ConversationDirTally",
    "IdleConversation",
    "list_idle_conversations",
]
