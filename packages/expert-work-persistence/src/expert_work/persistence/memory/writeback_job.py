"""B-168 —— 长期记忆后台写回的任务表(``memory_writeback_job``)。

一轮结束时只落一行**指针**(哪个会话、哪个检查点、哪个 run),控制面的
``MemoryWritebackWorker`` 领走后读检查点、调记忆模型、写记忆。和
``memory_writeback_dlq`` 是两套状态机:DLQ 存的是已经抽好的内容、跳过模型直接重写;
这里存的是 run 指针、要调模型(设计稿 §4)。

状态:``pending`` → ``running``(领取)→ ``done`` / ``failed``;``running`` 失败未到上限
退回 ``pending``。

**领取规则**(:meth:`MemoryWritebackJobStore.claim_next`,内存版与 SQL 版逐字同义):

* 可领 = ``pending``,或 ``running`` 但租约已过期(``lease_until <= now``)且
  ``attempts < max_attempts``(领取者崩了,别的副本接手)。
* 同一 (租户, 用户) 串行:该用户有别的 ``running`` 且租约未过期的任务时不领;该用户有
  更早的(按 ``(created_at, id)``)``pending`` / ``running`` 任务时也不领 —— 只有每个
  用户最早的那条未完成任务可领,所以失败重试的那条仍排在后来的任务前面。按用户而不是
  按会话:事实类记忆跨该用户所有智能体共享,两条去重合并同时跑会对同一批旧记忆各做一次
  增删改。
* 每次领取先把「租约过期且已到上限」的 ``running`` 收成 ``failed``,否则它永远挡着
  同一用户后面的任务。
* SQL 版只靠语句快照挡不住交错的领取者:A 已把 J1 改成 running 但未提交时,B 的快照里
  J1 还是 pending,一条 ``created_at`` 更早(副本时钟偏差)的 J0 就不被任何规则挡住。
  所以选出候选后先拿该 (租户, 用户) 的事务级 advisory lock,再用**新语句**重判上面两条
  规则 —— 新语句的快照看得见刚释放锁的那位已提交的结果。

**持有凭证**:``attempts`` 在领取时 +1,它同时是本次领取的凭证 —— :meth:`still_held` /
:meth:`renew_lease` / :meth:`finish` / :meth:`fail_attempt` 都要带上领取时拿到的
``attempts``,行已被别人重新领走(租约过期后)或已被清除时它们不生效,迟到的旧持有者
不会覆盖新持有者的结果。

**每条任务至少有一个指针**:``checkpoint_id`` 或 ``message_count``(表上有 CHECK,
:meth:`~MemoryWritebackJobStore.enqueue` 也先拒)—— 处理器绝不去读整个会话。

**收尾**分两种:

* :meth:`finish` —— 处理器跑完了(包括记忆写回自己报「没写成」:那种情况抽取出的内容
  已经交给 DLQ 重试,或是被注入扫描拦下、重试也一样),一律 ``done``,结果记在行上。
* :meth:`fail_attempt` —— 处理器抛错(取配置 / 建路由 / 读检查点失败、超时)。未到上限
  退回 ``pending`` 等下一次领取;到上限收成 ``failed``。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from expert_work.persistence.models import MemoryWritebackJobRow

PENDING: Final = "pending"
RUNNING: Final = "running"
DONE: Final = "done"
FAILED: Final = "failed"

#: 还没结束的状态 —— 积压数、「更早的未完成任务」都按它算。
_UNFINISHED: Final = (PENDING, RUNNING)


@dataclass(frozen=True)
class MemoryWritebackJob:
    """One row of ``memory_writeback_job``."""

    id: UUID
    tenant_id: UUID
    user_id: UUID
    agent_name: str
    agent_version: str
    thread_id: UUID
    checkpoint_id: str | None
    message_count: int | None
    run_id: UUID
    trace_id: str | None
    status: str
    attempts: int
    lease_until: datetime | None
    last_error: str | None
    written_count: int | None
    failed: bool | None
    queued_ms: int | None
    exec_ms: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _row_to_job(row: MemoryWritebackJobRow) -> MemoryWritebackJob:
    return MemoryWritebackJob(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        thread_id=row.thread_id,
        checkpoint_id=row.checkpoint_id,
        message_count=row.message_count,
        run_id=row.run_id,
        trace_id=row.trace_id,
        status=row.status,
        attempts=int(row.attempts),
        lease_until=row.lease_until,
        last_error=row.last_error,
        written_count=row.written_count,
        failed=row.failed,
        queued_ms=row.queued_ms,
        exec_ms=row.exec_ms,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _require_pointer(checkpoint_id: str | None, message_count: int | None) -> None:
    if checkpoint_id is None and message_count is None:
        msg = "a memory writeback job needs a checkpoint_id or message_count"
        raise ValueError(msg)


def _lease_expired_error(attempts: int) -> str:
    return f"lease expired after {attempts} attempt(s)"


class MemoryWritebackJobStore(abc.ABC):
    """Repository for background memory write-back jobs (see module docstring)."""

    @abc.abstractmethod
    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_name: str,
        agent_version: str,
        thread_id: UUID,
        run_id: UUID,
        trace_id: str | None,
        checkpoint_id: str | None = None,
        message_count: int | None = None,
        now: datetime,
    ) -> UUID:
        """Insert one ``pending`` job (``created_at = now``); return its id.

        Idempotent per turn pointer: a job with the same ``(run_id, checkpoint_id)``
        (NULL ``checkpoint_id`` counts as equal — migration 0163's unique index) is
        not inserted again; its existing id comes back. The write-back node may
        repeat an enqueue whose commit outlived its timeout, and orphan revival
        replays the node.

        :raises ValueError: neither ``checkpoint_id`` nor ``message_count`` given.
        """

    @abc.abstractmethod
    async def claim_next(
        self, *, now: datetime, lease_s: float, max_attempts: int
    ) -> MemoryWritebackJob | None:
        """Claim the oldest claimable job (module docstring) → ``running``,
        ``attempts + 1``, ``lease_until = now + lease_s``, ``started_at = now``.

        Cross-tenant: the caller wraps it in a bypass-RLS scope. Exactly one
        concurrent caller wins each job (SQL: ``FOR UPDATE SKIP LOCKED`` + CAS).
        """

    @abc.abstractmethod
    async def still_held(self, *, job_id: UUID, attempt: int) -> bool:
        """``True`` iff the row is still ``running`` under this claim (``attempts == attempt``)
        —— the processor's check right before each memory write: ``False`` once the job was
        purged, settled, or re-claimed by another worker after a lease expiry."""

    @abc.abstractmethod
    async def renew_lease(self, *, job_id: UUID, attempt: int, lease_until: datetime) -> bool:
        """Push the lease of a job this caller still holds; ``False`` = lost it."""

    @abc.abstractmethod
    async def finish(
        self,
        *,
        job_id: UUID,
        attempt: int,
        written_count: int,
        failed: bool,
        queued_ms: int,
        exec_ms: int,
        error: str | None,
        now: datetime,
    ) -> bool:
        """Settle a held job as ``done`` with its result; ``False`` = not held
        (re-claimed by someone else, already settled, or purged)."""

    @abc.abstractmethod
    async def fail_attempt(
        self,
        *,
        job_id: UUID,
        attempt: int,
        error: str,
        max_attempts: int,
        now: datetime,
    ) -> str | None:
        """Record a raised attempt: back to ``pending`` while ``attempts <
        max_attempts``, else ``failed``. Returns the new status, ``None`` when
        the job is not held."""

    @abc.abstractmethod
    async def get_by_run(self, *, tenant_id: UUID, run_id: UUID) -> MemoryWritebackJob | None:
        """The (latest) job a run produced — the console's background row (B2)."""

    @abc.abstractmethod
    async def exists(self, *, job_id: UUID) -> bool:
        """Whether the row is still there — the worker's purge check (§3.6)."""

    @abc.abstractmethod
    async def count_backlog(self) -> int:
        """``pending`` + ``running`` across tenants — the backlog gauge."""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Purge a user's jobs (any status); returns the count deleted."""

    @abc.abstractmethod
    async def delete_for_thread(self, *, tenant_id: UUID, thread_id: UUID) -> int:
        """Purge a conversation's jobs (any status); returns the count deleted."""


# ---------------------------------------------------------------------------
# In-memory implementation — unit tests
# ---------------------------------------------------------------------------


@dataclass
class InMemoryMemoryWritebackJobStore(MemoryWritebackJobStore):
    """Process-local store for tests. Predicates mirror the SQL ones verbatim."""

    _rows: dict[UUID, MemoryWritebackJob] = field(default_factory=dict)

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_name: str,
        agent_version: str,
        thread_id: UUID,
        run_id: UUID,
        trace_id: str | None,
        checkpoint_id: str | None = None,
        message_count: int | None = None,
        now: datetime,
    ) -> UUID:
        _require_pointer(checkpoint_id, message_count)
        for existing in self._rows.values():
            if (existing.run_id, existing.checkpoint_id) == (run_id, checkpoint_id):
                return existing.id
        job = MemoryWritebackJob(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            message_count=message_count,
            run_id=run_id,
            trace_id=trace_id,
            status=PENDING,
            attempts=0,
            lease_until=None,
            last_error=None,
            written_count=None,
            failed=None,
            queued_ms=None,
            exec_ms=None,
            created_at=now,
            started_at=None,
            finished_at=None,
        )
        self._rows[job.id] = job
        return job.id

    @staticmethod
    def _live(job: MemoryWritebackJob, now: datetime) -> bool:
        return job.status == RUNNING and job.lease_until is not None and job.lease_until > now

    @staticmethod
    def _claimable_self(job: MemoryWritebackJob, now: datetime, max_attempts: int) -> bool:
        if job.status == PENDING:
            return True
        return (
            job.status == RUNNING
            and job.lease_until is not None
            and job.lease_until <= now
            and job.attempts < max_attempts
        )

    def _blocked(self, job: MemoryWritebackJob, now: datetime) -> bool:
        for other in self._rows.values():
            if other.id == job.id or (other.tenant_id, other.user_id) != (
                job.tenant_id,
                job.user_id,
            ):
                continue
            if self._live(other, now):
                return True
            if other.status in _UNFINISHED and (other.created_at, other.id) < (
                job.created_at,
                job.id,
            ):
                return True
        return False

    async def claim_next(
        self, *, now: datetime, lease_s: float, max_attempts: int
    ) -> MemoryWritebackJob | None:
        for job in list(self._rows.values()):
            if (
                job.status == RUNNING
                and job.lease_until is not None
                and job.lease_until <= now
                and job.attempts >= max_attempts
            ):
                self._rows[job.id] = replace(
                    job,
                    status=FAILED,
                    lease_until=None,
                    finished_at=now,
                    last_error=_lease_expired_error(job.attempts),
                )
        candidates = sorted(
            (
                j
                for j in self._rows.values()
                if self._claimable_self(j, now, max_attempts) and not self._blocked(j, now)
            ),
            key=lambda j: (j.created_at, j.id),
        )
        if not candidates:
            return None
        claimed = replace(
            candidates[0],
            status=RUNNING,
            attempts=candidates[0].attempts + 1,
            lease_until=now + timedelta(seconds=lease_s),
            started_at=now,
        )
        self._rows[claimed.id] = claimed
        return claimed

    def _held(self, job_id: UUID, attempt: int) -> MemoryWritebackJob | None:
        job = self._rows.get(job_id)
        if job is None or job.status != RUNNING or job.attempts != attempt:
            return None
        return job

    async def still_held(self, *, job_id: UUID, attempt: int) -> bool:
        return self._held(job_id, attempt) is not None

    async def renew_lease(self, *, job_id: UUID, attempt: int, lease_until: datetime) -> bool:
        job = self._held(job_id, attempt)
        if job is None:
            return False
        self._rows[job_id] = replace(job, lease_until=lease_until)
        return True

    async def finish(
        self,
        *,
        job_id: UUID,
        attempt: int,
        written_count: int,
        failed: bool,
        queued_ms: int,
        exec_ms: int,
        error: str | None,
        now: datetime,
    ) -> bool:
        job = self._held(job_id, attempt)
        if job is None:
            return False
        self._rows[job_id] = replace(
            job,
            status=DONE,
            lease_until=None,
            written_count=written_count,
            failed=failed,
            queued_ms=queued_ms,
            exec_ms=exec_ms,
            last_error=error,
            finished_at=now,
        )
        return True

    async def fail_attempt(
        self,
        *,
        job_id: UUID,
        attempt: int,
        error: str,
        max_attempts: int,
        now: datetime,
    ) -> str | None:
        job = self._held(job_id, attempt)
        if job is None:
            return None
        if job.attempts >= max_attempts:
            self._rows[job_id] = replace(
                job, status=FAILED, lease_until=None, last_error=error, finished_at=now
            )
            return FAILED
        self._rows[job_id] = replace(job, status=PENDING, lease_until=None, last_error=error)
        return PENDING

    async def get_by_run(self, *, tenant_id: UUID, run_id: UUID) -> MemoryWritebackJob | None:
        rows = [j for j in self._rows.values() if j.tenant_id == tenant_id and j.run_id == run_id]
        return max(rows, key=lambda j: (j.created_at, j.id)) if rows else None

    async def exists(self, *, job_id: UUID) -> bool:
        return job_id in self._rows

    async def count_backlog(self) -> int:
        return sum(1 for j in self._rows.values() if j.status in _UNFINISHED)

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [
            jid
            for jid, j in self._rows.items()
            if j.tenant_id == tenant_id and j.user_id == user_id
        ]
        for jid in victims:
            del self._rows[jid]
        return len(victims)

    async def delete_for_thread(self, *, tenant_id: UUID, thread_id: UUID) -> int:
        victims = [
            jid
            for jid, j in self._rows.items()
            if j.tenant_id == tenant_id and j.thread_id == thread_id
        ]
        for jid in victims:
            del self._rows[jid]
        return len(victims)


# ---------------------------------------------------------------------------
# SQLAlchemy implementation — prod
# ---------------------------------------------------------------------------

_Job = MemoryWritebackJobRow


def _claimable_self(now: datetime, max_attempts: int) -> Any:
    return or_(
        _Job.status == PENDING,
        and_(
            _Job.status == RUNNING,
            _Job.lease_until <= now,
            _Job.attempts < max_attempts,
        ),
    )


def _blocked(now: datetime) -> Any:
    """``EXISTS``:同一 (租户, 用户) 的另一条任务正在跑(租约有效),或排在更早。

    与 ``InMemoryMemoryWritebackJobStore._blocked`` 逐字同义(契约场景钉住)。
    「更早」按 ``(created_at, id)``,并列时靠 id 定先后,同 B-139 ``_earlier_busy_exists``。
    """
    other = aliased(MemoryWritebackJobRow)
    return (
        select(other.id)
        .where(
            other.tenant_id == _Job.tenant_id,
            other.user_id == _Job.user_id,
            other.id != _Job.id,
            or_(
                and_(other.status == RUNNING, other.lease_until > now),
                and_(
                    other.status.in_(_UNFINISHED),
                    or_(
                        other.created_at < _Job.created_at,
                        and_(other.created_at == _Job.created_at, other.id < _Job.id),
                    ),
                ),
            ),
        )
        .exists()
    )


def _held(job_id: UUID, attempt: int) -> Any:
    return and_(_Job.id == job_id, _Job.status == RUNNING, _Job.attempts == attempt)


class SqlMemoryWritebackJobStore(MemoryWritebackJobStore):
    """Postgres-backed store. One short transaction per call.

    ``claim_lock_classid`` is the advisory-lock class for the per-(tenant, user) claim
    lock. The control plane owns the registry (``control_plane.advisory_locks``) and
    passes its value in — this package cannot import it.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, claim_lock_classid: int
    ) -> None:
        self._sf = session_factory
        self._claim_lock_classid = claim_lock_classid

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_name: str,
        agent_version: str,
        thread_id: UUID,
        run_id: UUID,
        trace_id: str | None,
        checkpoint_id: str | None = None,
        message_count: int | None = None,
        now: datetime,
    ) -> UUID:
        _require_pointer(checkpoint_id, message_count)
        insert_stmt = (
            pg_insert(_Job)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                agent_name=agent_name,
                agent_version=agent_version,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                message_count=message_count,
                run_id=run_id,
                trace_id=trace_id,
                status=PENDING,
                attempts=0,
                created_at=now,
            )
            # 唯一索引 ``(run_id, checkpoint_id) NULLS NOT DISTINCT``(0163):同一个
            # 指针已经有一行就不再插,回那一行的 id。
            .on_conflict_do_nothing()
            .returning(_Job.id)
        )
        async with self._sf() as session:
            job_id = (await session.execute(insert_stmt)).scalar_one_or_none()
            if job_id is None:
                job_id = (
                    await session.execute(
                        select(_Job.id).where(
                            _Job.run_id == run_id,
                            _Job.checkpoint_id.is_not_distinct_from(checkpoint_id),
                        )
                    )
                ).scalar_one()
            await session.commit()
        return job_id

    async def claim_next(
        self, *, now: datetime, lease_s: float, max_attempts: int
    ) -> MemoryWritebackJob | None:
        async with self._sf() as session:
            # 到上限的过期租约先收成 failed —— 否则它永远挡着同一用户后面的任务。单独一个
            # 事务:不把这些行锁带进下面等 advisory lock 的那段。
            await session.execute(
                update(_Job)
                .where(
                    _Job.status == RUNNING,
                    _Job.lease_until <= now,
                    _Job.attempts >= max_attempts,
                )
                .values(
                    status=FAILED,
                    lease_until=None,
                    finished_at=now,
                    last_error=func.concat("lease expired after ", _Job.attempts, " attempt(s)"),
                )
            )
            await session.commit()
        async with self._sf() as session:
            # ``FOR UPDATE SKIP LOCKED``:并发的领取者跳过别人已锁住的候选。
            candidate = (
                await session.execute(
                    select(_Job.id, _Job.tenant_id, _Job.user_id)
                    .where(_claimable_self(now, max_attempts), ~_blocked(now))
                    .order_by(_Job.created_at.asc(), _Job.id.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if candidate is None:
                await session.commit()
                return None
            job_id, tenant_id, user_id = candidate
            # 同一用户的领取者在这里排队(见模块 docstring);锁随事务提交释放。
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:cid, hashtext(:key))"),
                {"cid": self._claim_lock_classid, "key": f"{tenant_id}:{user_id}"},
            )
            # 新语句、新快照:拿到锁之后重判,看得见上一位持锁者已提交的领取。
            row = (
                (
                    await session.execute(
                        update(_Job)
                        .where(
                            _Job.id == job_id,
                            _claimable_self(now, max_attempts),
                            ~_blocked(now),
                        )
                        .values(
                            status=RUNNING,
                            attempts=_Job.attempts + 1,
                            lease_until=now + timedelta(seconds=lease_s),
                            started_at=now,
                        )
                        .returning(_Job)
                    )
                )
                .scalars()
                .first()
            )
            await session.commit()
        return _row_to_job(row) if row is not None else None

    async def still_held(self, *, job_id: UUID, attempt: int) -> bool:
        async with self._sf() as session:
            found = (await session.execute(select(_Job.id).where(_held(job_id, attempt)))).first()
        return found is not None

    async def renew_lease(self, *, job_id: UUID, attempt: int, lease_until: datetime) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(_Job).where(_held(job_id, attempt)).values(lease_until=lease_until)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def finish(
        self,
        *,
        job_id: UUID,
        attempt: int,
        written_count: int,
        failed: bool,
        queued_ms: int,
        exec_ms: int,
        error: str | None,
        now: datetime,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(_Job)
                .where(_held(job_id, attempt))
                .values(
                    status=DONE,
                    lease_until=None,
                    written_count=written_count,
                    failed=failed,
                    queued_ms=queued_ms,
                    exec_ms=exec_ms,
                    last_error=error,
                    finished_at=now,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def fail_attempt(
        self,
        *,
        job_id: UUID,
        attempt: int,
        error: str,
        max_attempts: int,
        now: datetime,
    ) -> str | None:
        exhausted = _Job.attempts >= max_attempts
        async with self._sf() as session:
            new_status = (
                await session.execute(
                    update(_Job)
                    .where(_held(job_id, attempt))
                    .values(
                        # SET 里的 ``attempts`` 读的是本行更新前的值。
                        status=case((exhausted, FAILED), else_=PENDING),
                        lease_until=None,
                        last_error=error,
                        finished_at=case((exhausted, now), else_=None),
                    )
                    .returning(_Job.status)
                )
            ).scalar_one_or_none()
            await session.commit()
        return str(new_status) if new_status is not None else None

    async def get_by_run(self, *, tenant_id: UUID, run_id: UUID) -> MemoryWritebackJob | None:
        stmt = (
            select(_Job)
            .where(_Job.tenant_id == tenant_id, _Job.run_id == run_id)
            .order_by(_Job.created_at.desc(), _Job.id.desc())
            .limit(1)
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalars().first()
        return _row_to_job(row) if row is not None else None

    async def exists(self, *, job_id: UUID) -> bool:
        async with self._sf() as session:
            found = (await session.execute(select(_Job.id).where(_Job.id == job_id))).first()
        return found is not None

    async def count_backlog(self) -> int:
        async with self._sf() as session:
            n = await session.scalar(
                select(func.count()).select_from(_Job).where(_Job.status.in_(_UNFINISHED))
            )
        return int(n or 0)

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        stmt = delete(_Job).where(_Job.tenant_id == tenant_id, _Job.user_id == user_id)
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_for_thread(self, *, tenant_id: UUID, thread_id: UUID) -> int:
        stmt = delete(_Job).where(_Job.tenant_id == tenant_id, _Job.thread_id == thread_id)
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "DONE",
    "FAILED",
    "PENDING",
    "RUNNING",
    "InMemoryMemoryWritebackJobStore",
    "MemoryWritebackJob",
    "MemoryWritebackJobStore",
    "SqlMemoryWritebackJobStore",
]
