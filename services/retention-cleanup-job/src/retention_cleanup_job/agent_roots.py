"""B-284 —— agent 根下 B-236 之前留下的存量,与冻结的 ``shared/``。

B-236 之后新对话都写进自己的 ``threads/<会话>/work/``,agent 根(``agents/<key>/``)里新出现的东西是
有意放进 ``/agent`` 的(记忆、模板、脚本……),永不按时间删。之前的老视图对话直接写在 agent 根,
那是一批**固定的存量**。

**切换时刻 ``T`` 钉死在配置里**(``EXPERT_WORK_RETENTION_AGENT_ROOT_CUTOVER``,带时区的 ISO 时间,
每个环境填 B-236 上线的时刻,写在 overlay 里;没设 = 整条不跑;晚于现在 = 写错了,记
``config_invalid``、不跑)。不从库里现算:清除最早的那段对话会让 ``min(created_at)`` 往后挪(多删),
老对话被迁成对话视图(B-262 P3)又会让它往前挪(功能悄悄关掉)。

**「早于 T」与「满天数」都看 ``max(st_mtime, st_ctime)``。** mtime 用户态随便设(``unzip`` /
``tar`` / ``cp -p`` / ``rsync -a`` / ``mv`` / ``touch -d``,开场复制自己也保留 mtime),T 之后放进
``/agent`` 的文件完全可以带着老 mtime;ctime 用户态改不了,只会往「更新」那边走 —— 判错的方向是少删。

* **顶层文件**:``max(mtime, ctime)`` 早于 ``T``、也早于「现在 - 租户的 ``artifact_retention_days``
  (默认 90)」、没有登记成产物(任何一版指着它)→ 删。``T`` 之前解压出来、带着几年前 mtime 的
  文件,从写下那天起满天数才删。删之前那一次 ``lstat`` 再核对:还是判定时那个 ``(st_dev, st_ino)``、
  ``max(mtime, ctime)`` 还早于 ``T``(``remove_entry(expect=…, accept=…)``),不是就不删
  (``skipped_changed``)。
* **子目录**:整棵树(含目录自己)每一项的 ``max(mtime, ctime)`` 都早于 ``T``(碰到第一项不早于
  ``T`` 的就停)、里面没有登记成产物的文件、且这个用户在这个 agent 下闲置(最后一次 run 与这棵树
  最晚的那一项里较晚的那个)满同样天数 → 按判定时的 inode **改名挪进隔离区**
  ``{tenant}/.purged-agents/{user}/<原名>-<秒>-<随机>``(沙箱挂不到;满 7 天由隔离区那一条删,在那
  之前可以挪回),在那里再走一遍这棵树确认还都早于 ``T``(目录自己的 ctime 是改名时的,按 mtime
  看),不是就挪回原名(原名已被占就留在隔离区,计失败)。
* **永不碰**:点开头的名字(包括用户自己建的 ``.retention-hold-*``)、长期记忆文件(``AGENT_NOTES.md``、
  老视图投影的 ``MEMORY.md`` / ``PLAN.md`` / ``TODO.md``)、平台目录(``threads`` / ``uploads`` /
  ``inputs`` / ``.tool_results`` / ``skills`` / ``shared`` / ``agents``)、符号链接、``T`` 之后改过的
  任何东西、登记成产物的文件(B-63 管)。
* **``shared/``**(B-50 冻结的 legacy,沙箱里只读挂在 ``/workspace/shared``):
  ``EXPERT_WORK_RETENTION_SHARED_DELETE_AFTER`` 只认 ``YYYY-MM-DD``(按 UTC 零点),设了且已经过了,
  每个用户根下的 ``shared/`` 整个删掉;不设 / 空 = 永不删;数字、别的写法、早于这个功能上线的日期 →
  记错误、只跳过 ``shared/`` 这一步(整个作业照跑)。沙箱挂载与 ``shared:`` 前缀在目录不在时本来就
  容忍(``exec_view`` 不挂、文件工具读不到),所以代码留到删除之后另拆:到期之前照常能读。

路径一律逐段 ``O_NOFOLLOW`` 打开(:mod:`expert_work.persistence.workspace.safe_remove`)。每一项单独
``try``:失败计数(这条 pass 记失败),不影响别的。已软删的用户整棵跳过。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence import (
    PURGED_AGENTS_DIR,
    WORKSPACE_AGENTS_DIR,
    WORKSPACE_INPUTS_DIR,
    WORKSPACE_OVERFLOW_DIR,
    WORKSPACE_SHARED_DIR,
    WORKSPACE_SKILLS_DIR,
    WORKSPACE_UPLOADS_DIR,
)
from expert_work.persistence.models import (
    AgentRunRow,
    ArtifactVersionRow,
    TenantConfigRow,
    ThreadMetaRow,
)
from expert_work.persistence.workspace.safe_remove import (
    CHANGED,
    QUARANTINED,
    REMOVED,
    UNLINKED,
    quarantine_entry,
    remove_entry,
    restore_quarantined,
)
from expert_work.protocol.agent_key import sanitize_agent_key
from retention_cleanup_job.workspace_files import (
    THREADS_DIR,
    _agent_dirs,
    _uuid_dirs,
    deleted_marker,
)

logger = logging.getLogger(__name__)

#: agent 根下永不碰的名字:平台目录 + 长期记忆文件(新的索引与老视图投影的那几个)。点开头的另算。
RESERVED_NAMES = frozenset(
    {
        THREADS_DIR,
        WORKSPACE_UPLOADS_DIR,
        WORKSPACE_INPUTS_DIR,
        WORKSPACE_OVERFLOW_DIR,
        WORKSPACE_SKILLS_DIR,
        WORKSPACE_SHARED_DIR,
        WORKSPACE_AGENTS_DIR,
        "AGENT_NOTES.md",
        "MEMORY.md",
        "PLAN.md",
        "TODO.md",
    }
)
#: 判一棵子树「全在 T 之前」最多看多少项;超过就当「不知道」不删(只给一次遍历封顶)。
MAX_SUBTREE_ENTRIES = 5000
#: ``shared/`` 删除日期不许早于这一天(这个功能写成的日子):更早的日期只可能是写错了。
SHARED_DELETE_EARLIEST = date(2026, 11, 6)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_GONE = (REMOVED, UNLINKED, QUARANTINED)


@dataclass
class AgentRootTally:
    files_removed: int = 0
    dirs_removed: int = 0
    shared_removed: int = 0
    #: 子树项数超上限 / 读不了,不知道是不是全在 T 之前:不删。
    skipped_unknown: int = 0
    #: agent 根顶层的符号链接:不跟、不删。
    skipped_symlinks: int = 0
    #: 判定之后、删之前被换掉(inode 变了 / 复查不过):不删。
    skipped_changed: int = 0
    failed: int = 0

    @property
    def needs_attention(self) -> bool:
        return self.failed > 0


def parse_cutover(raw: str | None) -> tuple[datetime | None, str | None]:
    """``EXPERT_WORK_RETENTION_AGENT_ROOT_CUTOVER`` → ``(时刻, 问题)``。

    没设 / 空 → ``(None, None)``;必须是带时区的 ISO 时间(``2026-10-30T23:48:00+08:00``),
    纯数字、没时区、解析不了都算问题。"""
    if raw is None or not raw.strip():
        return None, None
    text = raw.strip()
    if text.isdigit():
        return None, "a bare number is not an ISO datetime"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None, "not an ISO datetime"
    if when.tzinfo is None:
        return None, "the datetime has no timezone"
    return when, None


def parse_shared_delete_after(raw: str | None) -> tuple[datetime | None, str | None]:
    """``EXPERT_WORK_RETENTION_SHARED_DELETE_AFTER`` → ``(UTC 零点, 问题)``。只认 ``YYYY-MM-DD``,
    且不早于 :data:`SHARED_DELETE_EARLIEST`;没设 / 空 → ``(None, None)``。"""
    if raw is None or not raw.strip():
        return None, None
    text = raw.strip()
    if not _DATE_RE.fullmatch(text):
        return None, "expected YYYY-MM-DD"
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None, "not a calendar date"
    if day < SHARED_DELETE_EARLIEST:
        return None, f"earlier than {SHARED_DELETE_EARLIEST.isoformat()}"
    return datetime(day.year, day.month, day.day, tzinfo=UTC), None


@dataclass(frozen=True)
class _Context:
    cutover: float
    now: float
    default_days: int
    tenant_days: dict[UUID, int]


async def _tenant_days(sf: async_sessionmaker[AsyncSession]) -> dict[UUID, int]:
    async with sf() as session:
        rows = (
            await session.execute(
                select(TenantConfigRow.tenant_id, TenantConfigRow.artifact_retention_days).where(
                    TenantConfigRow.artifact_retention_days.is_not(None)
                )
            )
        ).all()
    return {tenant: int(days) for tenant, days in rows}


async def _registered(sf: async_sessionmaker[AsyncSession], tenant: UUID, user: UUID) -> set[str]:
    """这个用户登记过的产物路径(任何一版,不论产物是否软删)。"""
    async with sf() as session:
        rows = (
            await session.execute(
                select(ArtifactVersionRow.path_in_workspace)
                .where(ArtifactVersionRow.tenant_id == tenant, ArtifactVersionRow.user_id == user)
                .distinct()
            )
        ).scalars()
        return set(rows)


async def _last_runs(
    sf: async_sessionmaker[AsyncSession], tenant: UUID, user: UUID
) -> dict[str, float]:
    """这个用户在每个 agent 下最后一次 run 的时间(unix 秒),按
    ``sanitize_agent_key(agent_name)`` 归到 agent 根。"""
    stmt = (
        select(ThreadMetaRow.agent_name, func.max(AgentRunRow.created_at))
        .join(
            AgentRunRow,
            (AgentRunRow.thread_id == ThreadMetaRow.thread_id)
            & (AgentRunRow.tenant_id == ThreadMetaRow.tenant_id),
        )
        .where(
            ThreadMetaRow.tenant_id == tenant,
            ThreadMetaRow.user_id == user,
            ThreadMetaRow.agent_name.is_not(None),
        )
        .group_by(ThreadMetaRow.agent_name)
    )
    async with sf() as session:
        rows = (await session.execute(stmt)).all()
    out: dict[str, float] = {}
    for name, at in rows:
        key = sanitize_agent_key(name)
        out[key] = max(out.get(key, 0.0), at.timestamp())
    return out


def _touched(info: os.stat_result) -> float:
    """最后一次被改的时刻:mtime 用户态能设,ctime 不能 —— 取两者较晚的那个。"""
    return max(info.st_mtime, info.st_ctime)


def _subtree_touched(
    path: Path, *, top_ctime: bool = True, stop_at: float | None = None
) -> float | None:
    """整棵树(含它自己)最晚的 ``max(mtime, ctime)``;不跟链接;项数超过上限或读不了 → ``None``
    (不知道)。``top_ctime=False``:目录自己只看 mtime(它的 ctime 是我们改名时的)。``stop_at``:
    一碰到不早于它的项就停、回那一项的时刻(判「全在 T 之前」用不着走完整棵树)。"""
    try:
        top = os.lstat(path)
    except OSError:
        return None
    newest = _touched(top) if top_ctime else top.st_mtime
    if stop_at is not None and newest >= stop_at:
        return newest
    stack = [path]
    seen = 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > MAX_SUBTREE_ENTRIES:
                        return None
                    info = entry.stat(follow_symlinks=False)
                    newest = max(newest, _touched(info))
                    if stop_at is not None and newest >= stop_at:
                        return newest
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
        except OSError:
            return None
    return newest


def _parts(tenant: UUID, user: UUID, key: str) -> list[str]:
    return [str(tenant), str(user), WORKSPACE_AGENTS_DIR, key]


def _remove_file(
    root: str, tenant: UUID, user: UUID, key: str, name: str, info: os.stat_result, cutover: float
) -> str:
    """按判定时的 inode 删;删之前那一次 ``lstat`` 再要求 ``max(mtime, ctime)`` 还早于 ``T``。"""
    return remove_entry(
        root,
        _parts(tenant, user, key),
        name,
        tenant_id=tenant,
        owner_id=user,
        expect=(info.st_dev, info.st_ino),
        accept=lambda now: _touched(now) < cutover,
    )


def _remove_dir(
    root: str,
    tenant: UUID,
    user: UUID,
    key: str,
    name: str,
    info: os.stat_result,
    cutover: float,
) -> str:
    """按判定时的 inode 改名挪进隔离区 → 在那里再走一遍这棵树(还都早于 ``T`` 吗)→ 不是就挪回原处。

    隔离区 ``{tenant}/.purged-agents/{user}/`` 沙箱挂不到;留下的满 7 天由隔离区那一条删,在那之前能
    挪回。返回 :data:`QUARANTINED` / :data:`CHANGED`(或 :func:`quarantine_entry` 的别的结果)。"""
    parts = _parts(tenant, user, key)
    outcome, parked = quarantine_entry(
        root, parts, name, tenant_id=tenant, owner_id=user, expect=(info.st_dev, info.st_ino)
    )
    if outcome != QUARANTINED or parked is None:
        return outcome
    held = Path(root) / str(tenant) / PURGED_AGENTS_DIR / str(user) / parked
    newest = _subtree_touched(held, top_ctime=False, stop_at=cutover)
    if newest is not None and newest < cutover:
        return QUARANTINED
    if restore_quarantined(root, parts, name, tenant_id=tenant, owner_id=user, parked=parked):
        return CHANGED
    msg = f"{name!r} was recreated while held; left in quarantine as {parked!r}"
    raise OSError(msg)


def _sweep_agent_root(
    root: str,
    tenant: UUID,
    user: UUID,
    key: str,
    agent_dir: Path,
    ctx: _Context,
    registered: set[str],
    last_run: float,
    tally: AgentRootTally,
) -> None:
    """同步:一个 agent 根的顶层。"""
    days = ctx.tenant_days.get(tenant, ctx.default_days)
    horizon = ctx.now - days * 86400
    prefix = f"{WORKSPACE_AGENTS_DIR}/{key}/"
    try:
        entries = sorted(os.scandir(agent_dir), key=lambda e: e.name)
    except OSError:
        tally.failed += 1
        return
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in RESERVED_NAMES:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                tally.skipped_symlinks += 1
            elif stat.S_ISREG(info.st_mode):
                touched = _touched(info)
                if touched < ctx.cutover and touched < horizon and prefix + name not in registered:
                    outcome = _remove_file(root, tenant, user, key, name, info, ctx.cutover)
                    tally.files_removed += int(outcome in _GONE)
                    tally.skipped_changed += int(outcome == CHANGED)
            elif stat.S_ISDIR(info.st_mode):
                if any(p.startswith(f"{prefix}{name}/") for p in registered):
                    continue
                newest = _subtree_touched(Path(entry.path), stop_at=ctx.cutover)
                if newest is None:
                    tally.skipped_unknown += 1
                    continue
                if newest < ctx.cutover and max(newest, last_run) < horizon:
                    outcome = _remove_dir(root, tenant, user, key, name, info, ctx.cutover)
                    tally.dirs_removed += int(outcome == QUARANTINED)
                    tally.skipped_changed += int(outcome == CHANGED)
        except Exception as exc:
            tally.failed += 1
            logger.warning(
                "retention.agent_root_entry_failed tenant=%s user=%s error=%s",
                tenant,
                user,
                type(exc).__name__,
            )


def _users(root: str) -> Iterable[tuple[UUID, UUID, Path]]:
    for tenant, tenant_dir in _uuid_dirs(Path(root)):
        for user, user_dir in _uuid_dirs(tenant_dir):
            if deleted_marker(root, tenant, user).exists():
                continue
            yield tenant, user, user_dir


async def sweep_agent_roots(
    sf: async_sessionmaker[AsyncSession],
    root: str,
    *,
    cutover: datetime,
    default_days: int,
    tally: AgentRootTally,
    now: datetime | None = None,
) -> AgentRootTally:
    """顶层文件与子目录两条规则,``cutover`` = 钉死的切换时刻 ``T``。

    L2 —— ``T`` 晚于现在只可能是写错了(切换时刻是过去的事;钉在将来等于把将来写进 ``/agent`` 的
    东西都当存量):记 ``retention.config_invalid``、这一步不跑、记失败。"""
    current = now or datetime.now(UTC)
    if cutover > current:
        logger.error(
            "retention.config_invalid env=EXPERT_WORK_RETENTION_AGENT_ROOT_CUTOVER value=%r "
            "problem=later than now",
            cutover.isoformat(),
        )
        tally.failed += 1
        return tally
    ctx = _Context(
        cutover=cutover.timestamp(),
        now=current.timestamp(),
        default_days=default_days,
        tenant_days=await _tenant_days(sf),
    )
    for tenant, user, user_dir in await asyncio.to_thread(lambda: list(_users(root))):
        agents = await asyncio.to_thread(_agent_dirs, user_dir)
        if not agents:
            continue
        try:
            registered = await _registered(sf, tenant, user)
            last_runs = await _last_runs(sf, tenant, user)
        except Exception:
            tally.failed += 1
            logger.warning("retention.agent_roots_lookup_failed tenant=%s", tenant, exc_info=True)
            continue
        for key, agent_dir in agents:
            await asyncio.to_thread(
                _sweep_agent_root,
                root,
                tenant,
                user,
                key,
                agent_dir,
                ctx,
                registered,
                last_runs.get(key, 0.0),
                tally,
            )
    return tally


def sweep_shared(
    root: str, *, delete_after: datetime | None, now: datetime, tally: AgentRootTally
) -> None:
    """同步:``delete_after`` 设了且已过 → 每个用户根下的 ``shared/`` 整个删掉。"""
    if delete_after is None or now < delete_after:
        return
    for tenant, user, _user_dir in _users(root):
        try:
            outcome = remove_entry(
                root,
                [str(tenant), str(user)],
                WORKSPACE_SHARED_DIR,
                tenant_id=tenant,
                owner_id=user,
            )
        except Exception as exc:
            tally.failed += 1
            logger.warning(
                "retention.shared_remove_failed tenant=%s user=%s error=%s",
                tenant,
                user,
                type(exc).__name__,
            )
            continue
        tally.shared_removed += int(outcome in _GONE)


__all__ = [
    "MAX_SUBTREE_ENTRIES",
    "RESERVED_NAMES",
    "SHARED_DELETE_EARLIEST",
    "AgentRootTally",
    "parse_cutover",
    "parse_shared_delete_after",
    "sweep_agent_roots",
    "sweep_shared",
]
