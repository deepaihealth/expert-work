"""B-285 —— 删一条对话的轨迹对象(L7 trajectory:每个结束的 run 写一个,内容是整段对话)。

三处彻底删除(对话 / 用户 / 智能体)共用,都在删 run 行**之前**调用:键里的日期只能从 run 的
时间推出来(:mod:`expert_work.protocol.trajectory_keys`),run 行没了就推不出来了。

每个 run 推出它可能落在的那几天(``created_at`` 到 ``finished_at``,没收尾的用 ``updated_at``,
两头各放 :data:`~expert_work.protocol.trajectory_keys.KEY_DATE_SKEW`),对每一天的四种结果各
列一次 ``…/{日期}/{对话}`` 这个前缀 —— 正好是这条对话自己的对象和它名下子智能体的对象
(``{对话}.jsonl`` 与 ``{对话}/…``;UUID 定长,碰不到别的对话)—— 再逐个删。从不按整个租户
列。

还在跑、删除之后才收尾的 run 会再写一个对象(删除不停 run);留存作业的孤儿扫描按「对话行
已不在」把它删掉。有对象删不掉 → 抛 :class:`TrajectoriesNotDeletedError`,调用方按各自的规矩
留着对话行等重跑。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from expert_work.protocol.trajectory_keys import (
    TRAJECTORY_OUTCOMES,
    TRAJECTORY_PREFIX,
    trajectory_days,
    union_days,
)
from expert_work.runtime.storage import ObjectStore

logger = logging.getLogger("expert_work.control_plane.purge.trajectories")


class TrajectoriesNotDeletedError(Exception):
    """有轨迹对象没列出来或没删掉;对话行留着,等重跑。``removed`` = 这一次已经删掉的个数。"""

    def __init__(self, removed: int) -> None:
        super().__init__(f"{removed} trajectory objects deleted, some could not be")
        self.removed = removed


class _RunTimes(Protocol):
    @property
    def created_at(self) -> datetime: ...
    @property
    def updated_at(self) -> datetime: ...
    @property
    def finished_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class TrajectoryStore:
    """Where the run path writes trajectories (``runtime.trajectory_recorder``)."""

    object_store: ObjectStore
    prefix: str = TRAJECTORY_PREFIX


def run_days(runs: Sequence[_RunTimes]) -> list[date]:
    """Every UTC day the trajectories of ``runs`` (and their sub-agents) can carry."""
    return union_days(
        trajectory_days(created_at=r.created_at, ended_at=r.finished_at or r.updated_at)
        for r in runs
    )


def today() -> list[date]:
    """The days a run still finishing right now would write to (late-pass days)."""
    now = datetime.now(UTC)
    return trajectory_days(created_at=now, ended_at=now)


async def delete_conversation_trajectories(
    store: TrajectoryStore | None,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    days: Sequence[date],
) -> int:
    """Delete the conversation's trajectory objects dated ``days``; returns how
    many objects were deleted. ``store=None`` (no object store wired, so the run
    path never wrote any) → 0."""
    if store is None:
        return 0
    removed = failed = 0
    for day in days:
        for outcome in TRAJECTORY_OUTCOMES:
            scan = f"{store.prefix}/{tenant_id}/{outcome}/{day:%Y}/{day:%m}/{day:%d}/{thread_id}"
            try:
                keys = await store.object_store.list_prefix(scan)
            except Exception as exc:
                failed += 1
                logger.warning("purge.trajectory_list_failed error=%s", type(exc).__name__)
                continue
            for key in keys:
                try:
                    await store.object_store.delete(key)
                except Exception as exc:
                    failed += 1
                    logger.warning("purge.trajectory_delete_failed error=%s", type(exc).__name__)
                    continue
                removed += 1
    if failed:
        raise TrajectoriesNotDeletedError(removed)
    return removed


def trajectory_store_for(runtime: object) -> TrajectoryStore | None:
    """The store the run path writes to: ``runtime.trajectory_recorder`` (set in
    ``app.py`` when an object store is configured), else ``None``."""
    recorder = getattr(runtime, "trajectory_recorder", None)
    if recorder is None:
        return None
    return TrajectoryStore(object_store=recorder.object_store, prefix=recorder.prefix)
