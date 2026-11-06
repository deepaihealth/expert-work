"""B-285 —— 删一条对话的轨迹对象(L7 trajectory:每个结束的 run 写一个,内容是整段对话)。

三处彻底删除(对话 / 用户 / 智能体)共用,都在删 run 行**之前**调用:键里的日期只能从 run 的
时间推出来(:mod:`expert_work.protocol.trajectory_keys`),run 行没了就推不出来了。没有 run 行
的对话退回对话行自己的 ``created_at`` 到 ``updated_at``(同一个天数上限);两样都没有的记一次
「推不出日期」(:attr:`TrajectoryDeletion.undatable`),不当作删干净了。

每一天、每种结果是一个单元,并发跑(最多 :data:`CONCURRENCY` 个):

* 这条对话自己的对象,键是确定的(:func:`~expert_work.protocol.trajectory_keys.trajectory_key`)
  —— 直接删,不列。对象存储的删除对不存在的键也算成功(``ObjectStore.delete`` 的约定),
  所以这一项数的是「清掉的键」,不是「之前确实存在的对象」。
* 它名下子智能体的对象在 ``…/{日期}/{对话}/`` 下(:func:`child_trajectory_prefix`),只有这
  一处要列,列到的逐个删。

从不按整个租户列。有删不掉 / 列不出的 → 抛 :class:`TrajectoriesNotDeletedError`(带这一次已经
清掉的个数),调用方按各自的规矩留着对话行等重跑。

还在跑、删除之后才收尾的 run 会再写一个对象(删除不停 run):三处删除在删掉对话行之后都对「今天」
再删一遍(:func:`today`)。再晚写的,只能靠留存作业的孤儿扫描 —— 那一条**依赖 B-286**(留存作业
能连上真对象存储)上线之后才真正在跑,在那之前没有兜底。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from expert_work.protocol.trajectory_keys import (
    TRAJECTORY_OUTCOMES,
    TRAJECTORY_PREFIX,
    child_trajectory_prefix,
    trajectory_days,
    trajectory_key,
    union_days,
)
from expert_work.runtime.storage import ObjectStore

logger = logging.getLogger("expert_work.control_plane.purge.trajectories")

#: 同时在跑的 (日期, 结果) 单元数上限。
CONCURRENCY = 8


class TrajectoriesNotDeletedError(Exception):
    """有轨迹对象没列出来或没删掉;对话行留着,等重跑。``removed`` = 这一次已经清掉的键数。"""

    def __init__(self, removed: int) -> None:
        super().__init__(f"{removed} trajectory keys cleared, some could not be")
        self.removed = removed


class _RunTimes(Protocol):
    @property
    def created_at(self) -> datetime: ...
    @property
    def updated_at(self) -> datetime: ...
    @property
    def finished_at(self) -> datetime | None: ...


class _ThreadTimes(Protocol):
    @property
    def created_at(self) -> datetime | None: ...
    @property
    def updated_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class TrajectoryStore:
    """Where the run path writes trajectories (``runtime.trajectory_recorder``)."""

    object_store: ObjectStore
    prefix: str = TRAJECTORY_PREFIX


@dataclass(frozen=True)
class ConversationDays:
    days: list[date]
    #: ``True`` = no run rows and no usable thread timestamps: nothing could be
    #: dated, so nothing was looked at (callers count it, never report it clean).
    undatable: bool = False


def run_days(runs: Sequence[_RunTimes]) -> list[date]:
    """Every UTC day the trajectories of ``runs`` (and their sub-agents) can carry."""
    return union_days(
        trajectory_days(created_at=r.created_at, ended_at=r.finished_at or r.updated_at)
        for r in runs
    )


def conversation_days(runs: Sequence[_RunTimes], meta: _ThreadTimes | None) -> ConversationDays:
    """:func:`run_days`; with no run rows, the conversation's own ``created_at`` ..
    ``updated_at`` (same day cap); with neither, :attr:`ConversationDays.undatable`."""
    if runs:
        return ConversationDays(run_days(runs))
    if meta is not None:
        end = meta.updated_at or meta.created_at
        start = meta.created_at or end
        if start is not None and end is not None:
            return ConversationDays(trajectory_days(created_at=start, ended_at=end))
    return ConversationDays([], undatable=True)


def today() -> list[date]:
    """The days a run still finishing right now would write to (late-pass days)."""
    now = datetime.now(UTC)
    return trajectory_days(created_at=now, ended_at=now)


async def _clear_unit(
    store: TrajectoryStore,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    day: date,
    outcome: str,
    gate: asyncio.Semaphore,
) -> tuple[int, int]:
    """One (day, outcome): the conversation's own key (deleted directly) and its
    sub-agents' (listed under ``{day}/{conversation}/``). ``(cleared, failed)``."""
    cleared = failed = 0
    async with gate:
        own = trajectory_key(
            tenant_id=tenant_id,
            outcome=outcome,
            at=datetime(day.year, day.month, day.day, tzinfo=UTC),
            thread_id=thread_id,
            prefix=store.prefix,
        )
        try:
            await store.object_store.delete(own)
            cleared += 1
        except Exception as exc:
            failed += 1
            logger.warning("purge.trajectory_delete_failed error=%s", type(exc).__name__)
        prefix = child_trajectory_prefix(
            tenant_id=tenant_id,
            outcome=outcome,
            day=day,
            conversation_id=thread_id,
            prefix=store.prefix,
        )
        try:
            children = await store.object_store.list_prefix(prefix)
        except Exception as exc:
            logger.warning("purge.trajectory_list_failed error=%s", type(exc).__name__)
            return cleared, failed + 1
        for key in children:
            try:
                await store.object_store.delete(key)
            except Exception as exc:
                failed += 1
                logger.warning("purge.trajectory_delete_failed error=%s", type(exc).__name__)
                continue
            cleared += 1
    return cleared, failed


async def delete_conversation_trajectories(
    store: TrajectoryStore | None,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    days: Sequence[date],
) -> int:
    """Clear the conversation's trajectory keys dated ``days``; returns the keys
    cleared (see the module docstring). ``store=None`` (no object store wired,
    so the run path never wrote any) → 0."""
    if store is None or not days:
        return 0
    gate = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(
        *(
            _clear_unit(
                store, tenant_id=tenant_id, thread_id=thread_id, day=day, outcome=outcome, gate=gate
            )
            for day in days
            for outcome in TRAJECTORY_OUTCOMES
        )
    )
    cleared = sum(c for c, _ in results)
    if any(f for _, f in results):
        raise TrajectoriesNotDeletedError(cleared)
    return cleared


async def delete_late_trajectories(
    store: TrajectoryStore | None, *, tenant_id: UUID, thread_id: UUID, log_event: str
) -> int:
    """The second pass after the thread row is gone: today's keys (a run still
    finishing). Best-effort — a failure is only logged."""
    try:
        return await delete_conversation_trajectories(
            store, tenant_id=tenant_id, thread_id=thread_id, days=today()
        )
    except Exception as exc:
        logger.warning("%s error=%s", log_event, type(exc).__name__)
        return exc.removed if isinstance(exc, TrajectoriesNotDeletedError) else 0


def trajectory_store_for(runtime: object) -> TrajectoryStore | None:
    """The store the run path writes to: ``runtime.trajectory_recorder`` (set in
    ``app.py`` when an object store is configured), else ``None``."""
    recorder = getattr(runtime, "trajectory_recorder", None)
    if recorder is None:
        return None
    return TrajectoryStore(object_store=recorder.object_store, prefix=recorder.prefix)
