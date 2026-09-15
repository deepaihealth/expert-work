"""``WorkspaceJanitorWorker`` —— 沙箱迁移波 3 PR-2(spec § 五)。

Periodic background worker driving the NAS-workspace housekeeping
phases: archiving soft-deleted user workspaces to the object store
(``_sweep_archives``), reclaiming expired ``agents/<key>/inputs/`` entries
(``_sweep_agent_inputs``, B-61 T12), refreshing per-user size accounting
(``_sweep_sizes``), and reaping stale ``_scratch`` sandbox-tmp directories
(``_sweep_scratch``). One cycle every ``interval_s`` (default 1800s = 30
minutes, spec § 五).

All phases are implemented: ``_sweep_archives`` uploads soft-deleted
users' workspaces then ``rm -rf``s the NAS directory (marking the row
archived), ``_sweep_agent_inputs`` expires injected-variable caches and
per-run directories by TTL, ``_sweep_sizes`` walks every tenant/user
directory and refreshes its size accounting, and ``_sweep_scratch`` reaps
stale ``_scratch`` sandbox-tmp directories. Structure mirrors
:class:`~control_plane.sandbox_reap_worker.SandboxReapWorker` (start/stop/
loop) and :class:`~control_plane.skill_curator.SkillCurator` (advisory-lock
wrapper around the cycle body).

No DLQ: a stale ``_scratch`` dir or a failed phase is retried on the next
cycle for free (both are idempotent — reaping an already-gone dir, or an
already-swept size, is a no-op). A losing replica on the advisory lock
silently skips the whole cycle (``JanitorRunStats(skipped=True)``); a single
failing phase logs and lets the remaining phases run (``_run_cycle``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.advisory_locks import WORKSPACE_JANITOR_LOCK_CLASSID
from control_plane.workspace_archive import (
    empty_tar_gz_bytes,
    stream_directory_tar_gz,
    workspace_archive_key,
)
from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.persistence.workspace.layout import (
    WORKSPACE_AGENTS_DIR,
    WORKSPACE_UPLOADS_DIR,
)
from expert_work.runtime.storage import ObjectStore
from orchestrator.tools.nas_workspace_store import DELETED_DIR, workspace_user_root

logger = logging.getLogger(__name__)

#: How often to run a full cycle. Spec § 五: 30 minutes.
_INTERVAL_S = 1800.0

#: ``stop()`` 等待当前这一轮 cycle 收尾的上限,超时就取消。照
#: ``SandboxReapWorker`` 的教训:**别用** ``interval_s + 5`` 这种公式——
#: 本 worker 的 interval 是分钟级,那个式子给出的「上界」比 K8s 默认 30s
#: 优雅期还长,等于没有上界。5 秒足够一轮正常 cycle 收尾;收不了尾就取
#: 消——三个阶段都是周期性、幂等的,下次启动会重来。
_STOP_TIMEOUT_S = 5.0

#: 这个超时守的不是「正常一轮该多久」,而是「一个挂死/泄漏的锁会话最多
#: 赖多久」——连接掉线本就会立即放锁,超时只在会话活着但卡住时兜底。真
#: 正的约束是:它必须是**任何**合理一轮 cycle 时长的上界,包括部署当天
#: 第一轮——PR-1→PR-2 之间攒的整批归档 backlog + 首次全树 du 一次性追
#: 平,时长和稳态后的 30 分钟一轮不是一个量级。60 分钟撑不住那一轮:命中
#: 后 PG 杀掉赢家的锁会话,锁释放,另一副本起并发 cycle(重复 multipart
#: 上传;tar 打包与 rmtree 赛跑,可能用半成品档案覆盖掉刚打完的完整档
#: 案),赢家侧 ``finally: rollback()`` 再报一个误导性的 ``cycle_failed``。
#: 改成 12 小时,把部署当天的 backlog 轮也罩住。曾考虑「每步 ping 一下续
#: 命」代替长超时,否决:单个用户的多 GiB 上传本身就可能撑爆任何按步长
#: 定的 ping 节奏假设,续命点找不到一个处处安全的粒度。
_LOCK_TXN_TIMEOUT_MS = 12 * 60 * 60 * 1000

#: spec § 五:临时沙箱寿命 ≤20min,72 倍余量。判据只看目录 mtime,不查 DB。
_SCRATCH_MAX_AGE_S = 24 * 3600.0

#: 与 orchestrator ``agent_sandbox._SCRATCH_DIR`` 同值(私名不跨包 import)。
_SCRATCH_DIR = "_scratch"

#: 与 orchestrator ``tools/inputs_doc.inputs_rel_dir`` 里的 ``inputs`` 同值
#: (私名不跨包 import,照上面 ``_SCRATCH_DIR`` 的先例)。
_INPUTS_DIR = "inputs"

#: 与 orchestrator ``tools/prefetch_script.CACHE_DIRNAME`` 同值 —— 内容寻址的
#: 共享缓存,挂在 ``inputs/`` 下、与 ``<run_id>/`` 平级。
_INPUTS_CACHE_DIR = "cache"

#: ``tools/prefetch_script._rewrite`` 的临时文件(``inputs.json`` + ``.tmp``)。
#: 它正常会被 ``os.replace`` 消费掉,预拉被 SIGKILL 时才遗留。
_INPUTS_TMP_NAME = "inputs.json.tmp"


@dataclass(frozen=True)
class _ReclaimPolicy:
    """一条回收策略。``enabled=False`` 的条目本批不生效,代码路径仍然走到。"""

    label: str  # 指标与日志用
    ttl_s: float
    enabled: bool

    def expired(self, mtime: float, now: float) -> bool:
        """这个条目该收了吗。

        ``enabled`` 判在**这里**、而不是在调用方用 ``if`` 跳掉整条策略:关着的
        策略照样被扫、照样逐条走到判定,只是答案恒为「不收」。B-63 要开 uploads
        与产物那两条时拨的是这个开关,不是补一段从没跑过的新代码。
        """
        return self.enabled and now - mtime >= self.ttl_s


#: B-61 T12 本批只启用 inputs 两条;uploads 与产物的条目**先放在这里但关着**——
#: 它们删的是用户数据,需要产品定 N、需要发布前告知、还要对外删除端点(B-62)当自救
#: 出口,那是 B-63 的事。机制一次写好,B-63 落地时是把开关拨开 + 接 touch 点,不是重写。
_POLICIES = (
    _ReclaimPolicy(label="inputs_cache", ttl_s=7 * 24 * 3600, enabled=True),
    _ReclaimPolicy(label="inputs_run_dir", ttl_s=7 * 24 * 3600, enabled=True),
    _ReclaimPolicy(label="uploads", ttl_s=90 * 24 * 3600, enabled=False),
    _ReclaimPolicy(label="artifacts", ttl_s=90 * 24 * 3600, enabled=False),
)


@dataclass(frozen=True)
class _ReclaimTarget:
    """一条策略在 agent 子树里的落点:``agents/<key>/<subdir>/`` 下收什么。

    只看这一层的直接子项(不递归):收的东西要么是一整棵 per-run 目录、要么是一个
    条目文件,再往下就是它们自己的内容。
    """

    subdir: str
    #: 收目录:只收名字是 UUID 的(per-run 目录)。``False`` = 这一层的目录一律不碰。
    uuid_dirs: bool
    #: 收文件:``None`` = 这一层的文件全收;否则只收名字在集合里的。
    file_names: frozenset[str] | None


#: 每条策略去哪儿收,键是 :attr:`_ReclaimPolicy.label`。
#:
#: ``artifacts`` **故意没有条目**:产物没有保留前缀 —— ``save_artifact`` 只是把
#: agent 子树里任意位置的一个文件**登记成一行**(``tools/artifact.py``),纯文件系统
#: 的清扫器认不出哪个文件是产物,``layout.py`` 的 ``WORKSPACE_RESERVED_PREFIXES``
#: 正是为此只列 machinery 与 uploads。所以「产物在哪」要等 B-63 先定义(它的方案是
#: 让目录自描述)。策略行留着是为了 TTL 与开关已经在位;**开关拨开之前还得先在这里
#: 补上落点**,否则它拨开也不收东西。
_TARGETS: dict[str, _ReclaimTarget] = {
    "inputs_cache": _ReclaimTarget(
        subdir=f"{_INPUTS_DIR}/{_INPUTS_CACHE_DIR}", uuid_dirs=False, file_names=None
    ),
    "inputs_run_dir": _ReclaimTarget(
        subdir=_INPUTS_DIR, uuid_dirs=True, file_names=frozenset({_INPUTS_TMP_NAME})
    ),
    "uploads": _ReclaimTarget(subdir=WORKSPACE_UPLOADS_DIR, uuid_dirs=False, file_names=None),
}


def _is_uuid_name(name: str) -> bool:
    try:
        UUID(name)
    except ValueError:
        return False
    return True


def _list_agent_dirs(user_dir: Path) -> list[Path]:
    """``<user_dir>/agents/`` 下的每棵 agent 子树。

    这一层的名字是 agent key(**不是 UUID**),所以复用不了 ``_list_uuid_dirs``。
    名字不做形状校验:路径是从 ``entry.path`` 直接拿的,不是拼字符串,``scandir``
    给的名字里不可能有 ``/``。symlink 不算目录(``follow_symlinks=False``):不顺
    着链接删。单目录扫描失败 log + 返回已收集的部分,照本文件既有口径。
    """
    agents_root = user_dir / WORKSPACE_AGENTS_DIR
    out: list[Path] = []
    try:
        with os.scandir(agents_root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        out.append(Path(entry.path))
                except OSError:
                    continue
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("workspace_janitor.agents_scan_failed path=%s", agents_root)
        return out
    return sorted(out)


def _reclaim(agent_dir: Path, policy: _ReclaimPolicy, now: float) -> tuple[int, int]:
    """按一条策略收一棵 agent 子树,返回 ``(删掉的文件数, 删掉的目录数)``。

    判据只有 mtime 一条(见 :meth:`WorkspaceJanitorWorker._sweep_agent_inputs`)。
    单条目失败 log + 继续:NAS 上并发写删是常态,一个 ESTALE 不该带走这一轮的其余
    条目。日志只记策略名与**目录**路径,不记条目名 —— uploads 的文件名是租户上传时
    带的,属于租户内容(spec §八)。
    """
    target = _TARGETS.get(policy.label)
    if target is None:
        return 0, 0
    base = agent_dir / target.subdir
    files = dirs = 0
    try:
        with os.scandir(base) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir:
                        # 只认 UUID 形状的目录名:别人建的目录不是垃圾。
                        if not target.uuid_dirs or not _is_uuid_name(entry.name):
                            continue
                    elif entry.is_file(follow_symlinks=False):
                        if target.file_names is not None and entry.name not in target.file_names:
                            continue
                    else:
                        continue  # symlink / 特殊文件:不是本平台造的,不碰
                    if not policy.expired(entry.stat(follow_symlinks=False).st_mtime, now):
                        continue
                except OSError:
                    continue
                try:
                    if is_dir:
                        shutil.rmtree(entry.path)
                        dirs += 1
                    else:
                        os.unlink(entry.path)
                        files += 1
                except OSError:
                    logger.warning(
                        "workspace_janitor.reclaim_failed policy=%s path=%s", policy.label, base
                    )
    except FileNotFoundError:
        return 0, 0  # 绝大多数 agent 根本没有这一层目录
    except OSError:
        logger.warning(
            "workspace_janitor.reclaim_scan_failed policy=%s path=%s", policy.label, base
        )
    return files, dirs


def _list_uuid_dirs(path: Path) -> list[tuple[UUID, Path]]:
    """列出 path 下目录名可解析为 UUID 的子目录——布局约定
    (workspace_user_root)之外的东西(_scratch/.deleted/lost+found/垃圾)
    天然被 UUID 解析挡掉。单目录扫描失败(NFS ESTALE、权限问题等 OSError)
    不炸整轮——log + 返回已收集到的部分,让调用方(``_sweep_sizes``)继续
    处理其余兄弟目录(照 Global Constraint「单目录/单用户失败 log + 继
    续」,呼应 ``workspace_quota._du`` 同样的容错口径)。"""
    out: list[tuple[UUID, Path]] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                try:
                    out.append((UUID(entry.name), Path(entry.path)))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("workspace_janitor.scan_failed path=%s", path)
        return out
    return sorted(out, key=lambda t: str(t[0]))


@dataclass
class JanitorRunStats:
    """One cycle's tally — returned by :meth:`WorkspaceJanitorWorker.run_once`."""

    archived: int = 0
    reharvested: int = 0
    refreshed: int = 0
    scratch_removed: int = 0
    #: ``_sweep_agent_inputs`` 这一轮收掉的条目数(按 :data:`_POLICIES` 全部策略合计)。
    inputs_files_removed: int = 0
    inputs_dirs_removed: int = 0
    skipped: bool = False


class WorkspaceJanitorWorker:
    """Runs the three-phase sweep on a timer, single-flight across replicas.

    Safe to deploy on every replica: a losing replica's
    ``pg_try_advisory_xact_lock`` attempt returns immediately with
    ``skipped=True`` rather than duplicating work (unlike
    ``SandboxReapWorker``, this worker's archive phase has a real side
    effect — an object-store upload — so duplicating it across replicas
    would be wasted GiB-scale transfer, not just wasted CPU; hence the lock,
    same call as ``SkillCurator``).
    """

    def __init__(
        self,
        *,
        user_workspaces: UserWorkspaceStore,
        quota_service: WorkspaceQuotaService,
        object_store: ObjectStore,
        workspace_root: str,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        interval_s: float = _INTERVAL_S,
    ) -> None:
        self._user_workspaces = user_workspaces
        self._quota_service = quota_service
        self._object_store = object_store
        self._workspace_root = workspace_root
        self._session_factory = session_factory
        self.interval_s = interval_s

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=_STOP_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    async def _loop(self) -> None:
        # No sweep at startup — a restart is exactly when in-flight uploads
        # / sandbox tmp dirs are most likely mid-flight, and nothing
        # degrades by waiting one interval (照 SandboxReapWorker)。
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except TimeoutError:
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("workspace_janitor.cycle_failed")

    async def run_once(self) -> JanitorRunStats:
        """Run one cycle, single-flight across replicas.

        ``session_factory=None`` (single-process / in-memory stack) skips
        the lock entirely — no cross-replica race to guard against. With a
        factory, a ``pg_try_advisory_xact_lock`` miss returns
        ``JanitorRunStats(skipped=True)`` immediately, no cycle attempted
        (照 ``SkillCurator.run_once``)。
        """
        if self._session_factory is None:
            stats = JanitorRunStats()
            await self._run_cycle(stats)
            return stats
        async with self._session_factory() as lock_session:
            # Long-hold guard: the lock txn stays open for the whole cycle;
            # keep it off any idle-in-transaction reaper.
            await lock_session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
            )
            got = (
                await lock_session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
                    {"cid": WORKSPACE_JANITOR_LOCK_CLASSID, "k": "workspace_janitor"},
                )
            ).scalar_one()
            if not got:
                await lock_session.rollback()
                return JanitorRunStats(skipped=True)
            stats = JanitorRunStats()
            try:
                await self._run_cycle(stats)
                return stats
            finally:
                # rollback ends the txn → releases the xact advisory lock.
                await lock_session.rollback()

    async def _run_cycle(self, stats: JanitorRunStats) -> None:
        # 回收排在 ``_sweep_sizes`` **之前**:那一步本来就逐用户跑全树 du,
        # 排在它前面回收,同一轮的体积记账天然反映回收量,零额外开销;排到最后
        # 就得再 du 一遍全树(见 test_reclaim_lands_in_the_same_cycle_size_accounting)。
        for phase in (
            self._sweep_archives,
            self._sweep_agent_inputs,
            self._sweep_sizes,
            self._sweep_scratch,
        ):
            try:
                await phase(stats)
            except Exception:  # 单阶段炸不拖累后续阶段;下轮自然重试
                logger.exception("workspace_janitor.phase_failed phase=%s", phase.__name__)

    async def _sweep_archives(self, stats: JanitorRunStats) -> None:
        """软删标记文件为发现源(``.deleted/{user}``,``user_purge`` 只
        落标记不碰 DB 行)——按 tenant 目录下 ``DELETED_DIR`` 里能解析成
        UUID 的条目逐用户归档。单用户失败 log + 继续,不拖累其余用户;标
        记文件本身永不删除(墓碑,见 :meth:`_archive_one`)。
        """
        root = Path(self._workspace_root)

        def _markers(tenant_dir: Path) -> list[UUID]:
            out: list[UUID] = []
            try:
                with os.scandir(tenant_dir / DELETED_DIR) as it:
                    for entry in it:
                        try:
                            out.append(UUID(entry.name))
                        except ValueError:
                            continue
            except FileNotFoundError:
                return []
            except OSError:
                logger.warning("workspace_janitor.marker_scan_failed path=%s", tenant_dir)
                return out
            return sorted(out, key=str)

        for tenant_id, tenant_dir in await asyncio.to_thread(_list_uuid_dirs, root):
            for user_id in await asyncio.to_thread(_markers, tenant_dir):
                try:
                    await self._archive_one(tenant_id, user_id, stats)
                except Exception:
                    logger.exception(
                        "workspace_janitor.archive_failed tenant=%s user=%s", tenant_id, user_id
                    )

    async def _archive_one(self, tenant_id: UUID, user_id: UUID, stats: JanitorRunStats) -> None:
        """spec § 4.1 + 硬要求①。单一路径:目录在就(重)归档;矩阵是推论。

        崩溃安全顺序:先传后删,mark 最后。已 mark 行的目录复活
        (上传路径不查软删标记,W2 既有设计)→ 覆盖上传同 key 再删,
        不重 mark——覆盖语义 runbook 有言在先。
        """
        ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        if ws.deleted_at is None:
            await self._user_workspaces.soft_delete(workspace_id=ws.id, now=datetime.now(UTC))
        key = workspace_archive_key(tenant_id, user_id, ws.id)
        user_dir = workspace_user_root(self._workspace_root, tenant_id, user_id)

        if await asyncio.to_thread(user_dir.is_dir):
            await self._object_store.put_stream(
                key, stream_directory_tar_gz(user_dir), content_type="application/gzip"
            )
            await asyncio.to_thread(shutil.rmtree, user_dir)
            if ws.archived_object_key is None:
                await self._user_workspaces.mark_archived(
                    workspace_id=ws.id, archived_object_key=key
                )
                stats.archived += 1
            else:
                stats.reharvested += 1
                logger.info("workspace_janitor.reharvested tenant=%s user=%s", tenant_id, user_id)
            return

        if ws.archived_object_key is not None:
            return  # 稳态墓碑:标记留着挡 acquire,行已收口
        if key not in await self._object_store.list_prefix(key):
            # 生前无目录(或上传前崩且目录本来就空缺)→ 统一产出空档案
            await self._object_store.put(key, empty_tar_gz_bytes(), content_type="application/gzip")
        await self._user_workspaces.mark_archived(workspace_id=ws.id, archived_object_key=key)
        stats.archived += 1

    async def _sweep_agent_inputs(self, stats: JanitorRunStats) -> None:
        """按 :data:`_POLICIES` 回收 ``agents/<agent_key>/`` 下的过期条目。

        发现源与 :meth:`_sweep_sizes` 同:tenant / user 两层 UUID 目录;再往下
        ``agents/`` 里是 agent key(**不是 UUID**),走 :func:`_list_agent_dirs`。

        **判据只看 mtime,不碰 atime**:NAS 多半挂成 ``noatime``/``relatime``,
        atime 不可信。cache 条目的 mtime 由预拉脚本维持成「最近一次下载时间」——
        24h 内命中不重下(mtime 不动)、超期命中会重下刷新 mtime —— 于是它就是
        「最近引用时间」的 24h 粒度近似;per-run 目录的 mtime 则被每次改写
        ``inputs.json`` 顶新,**活跃 run 的目录因此总是新鲜的**(删错它就打断了
        正在跑的那一轮,见 ``test_sweep_never_touches_a_recently_written_run_dir``)。

        ``now`` 取一次、整轮共用:同一轮里先后扫到的条目按同一条时间线判定。
        """
        root = Path(self._workspace_root)
        now = time.time()
        for _tenant_id, tenant_dir in await asyncio.to_thread(_list_uuid_dirs, root):
            for _user_id, user_dir in await asyncio.to_thread(_list_uuid_dirs, tenant_dir):
                for agent_dir in await asyncio.to_thread(_list_agent_dirs, user_dir):
                    for policy in _POLICIES:
                        files, dirs = await asyncio.to_thread(_reclaim, agent_dir, policy, now)
                        stats.inputs_files_removed += files
                        stats.inputs_dirs_removed += dirs

    async def _sweep_sizes(self, stats: JanitorRunStats) -> None:
        """文件系统为发现源:按 tenant/user 两层 UUID 目录全量扫,逐用户调
        :meth:`WorkspaceQuotaService.refresh`(建行 + 软删早退 + du +
        ``update_size``)。行不存在也扫——``refresh`` 自己会建行。单用户失败
        log + 继续,不拖累其余用户(NAS 并发写删场景常态)。
        """
        root = Path(self._workspace_root)
        for tenant_id, tenant_dir in await asyncio.to_thread(_list_uuid_dirs, root):
            for user_id, _user_dir in await asyncio.to_thread(_list_uuid_dirs, tenant_dir):
                try:
                    await self._quota_service.refresh(tenant_id=tenant_id, user_id=user_id)
                    stats.refreshed += 1
                except Exception:
                    logger.exception(
                        "workspace_janitor.refresh_failed tenant=%s user=%s", tenant_id, user_id
                    )

    async def _sweep_scratch(self, stats: JanitorRunStats) -> None:
        scratch_root = Path(self._workspace_root) / _SCRATCH_DIR

        def _stale_dirs() -> list[Path]:
            cutoff = time.time() - _SCRATCH_MAX_AGE_S
            out: list[Path] = []
            try:
                with os.scandir(scratch_root) as it:
                    for entry in it:
                        try:
                            if (
                                entry.is_dir(follow_symlinks=False)
                                and entry.stat(follow_symlinks=False).st_mtime < cutoff
                            ):
                                out.append(Path(entry.path))
                        except OSError:
                            continue
            except FileNotFoundError:
                return []
            return out

        for path in await asyncio.to_thread(_stale_dirs):
            try:
                await asyncio.to_thread(shutil.rmtree, path)
                stats.scratch_removed += 1
            except OSError:
                logger.warning("workspace_janitor.scratch_remove_failed path=%s", path)


__all__ = ["JanitorRunStats", "WorkspaceJanitorWorker"]
