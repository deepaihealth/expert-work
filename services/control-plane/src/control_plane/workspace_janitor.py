"""``WorkspaceJanitorWorker`` —— 沙箱迁移波 3 PR-2(spec § 五)。

Periodic background worker driving the three NAS-workspace housekeeping
phases: archiving soft-deleted user workspaces to the object store
(``_sweep_archives``), refreshing per-user size accounting
(``_sweep_sizes``), and reaping stale ``_scratch`` sandbox-tmp directories
(``_sweep_scratch``). One cycle every ``interval_s`` (default 1800s = 30
minutes, spec § 五).

This task (Task 3) lands the worker skeleton, the advisory-lock single-flight
wrapper, and the ``_scratch`` phase only — ``_sweep_archives`` /
``_sweep_sizes`` are ``pass`` stubs, filled by Task 4 / Task 5. Structure
mirrors :class:`~control_plane.sandbox_reap_worker.SandboxReapWorker`
(start/stop/loop) and :class:`~control_plane.skill_curator.SkillCurator`
(advisory-lock wrapper around the cycle body).

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
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.runtime.storage import ObjectStore

logger = logging.getLogger(__name__)

#: How often to run a full cycle. Spec § 五: 30 minutes.
_INTERVAL_S = 1800.0

#: ``stop()`` 等待当前这一轮 cycle 收尾的上限,超时就取消。照
#: ``SandboxReapWorker`` 的教训:**别用** ``interval_s + 5`` 这种公式——
#: 本 worker 的 interval 是分钟级,那个式子给出的「上界」比 K8s 默认 30s
#: 优雅期还长,等于没有上界。5 秒足够一轮正常 cycle 收尾;收不了尾就取
#: 消——三个阶段都是周期性、幂等的,下次启动会重来。
_STOP_TIMEOUT_S = 5.0

#: Advisory-lock classid for the single-flight janitor cycle. Registry (each
#: value distinct so no two locks ever share a key): ``workspace_lock.py``
#: uses 1, ``mcp_oauth_refresh_lock.py`` uses 2,
#: ``quality_drift_worker.py`` uses 8615, ``memory_consolidator.py`` uses
#: 8616, ``skill_curator.py`` uses 8617, ``_tenant_resource_lock.py`` uses
#: 8618, and this worker uses 8619 — two-arg ``(int4, int4)`` classid space.
_JANITOR_LOCK_CLASSID = 8619

#: drift 用 5 分钟;janitor 的归档上传是 GiB 级长活,5 分钟会把持锁会话
#: 半路杀掉(idle_in_transaction 判定的是锁会话,不是干活协程)。
#: 60 分钟 = interval x 2,孤锁仍有界。
_LOCK_TXN_TIMEOUT_MS = 60 * 60 * 1000

#: spec § 五:临时沙箱寿命 ≤20min,72 倍余量。判据只看目录 mtime,不查 DB。
_SCRATCH_MAX_AGE_S = 24 * 3600.0

#: 与 orchestrator ``agent_sandbox._SCRATCH_DIR`` 同值(私名不跨包 import)。
_SCRATCH_DIR = "_scratch"


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
                    {"cid": _JANITOR_LOCK_CLASSID, "k": "workspace_janitor"},
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
        for phase in (self._sweep_archives, self._sweep_sizes, self._sweep_scratch):
            try:
                await phase(stats)
            except Exception:  # 单阶段炸不拖累后续阶段;下轮自然重试
                logger.exception("workspace_janitor.phase_failed phase=%s", phase.__name__)

    async def _sweep_archives(self, stats: JanitorRunStats) -> None:
        """Task 4."""

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
