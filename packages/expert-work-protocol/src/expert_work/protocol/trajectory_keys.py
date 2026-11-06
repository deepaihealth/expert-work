"""B-285 —— 轨迹对象(L7 trajectory)在对象存储里的键,唯一的拼法与解析。

写的一方是 ``orchestrator.trajectory.recorder.TrajectoryRecorder``(每个结束的 run 写一个
对象,内容是整段对话);删的一方是三处彻底删除(对话 / 用户 / 智能体)与留存作业。几方
都从这里拿键,不各自拼字符串。

键::

    {prefix}/{tenant}/{outcome}/{YYYY}/{MM}/{DD}/{thread}.jsonl              对话本身的 run
    {prefix}/{tenant}/{outcome}/{YYYY}/{MM}/{DD}/{conversation}/{thread}.jsonl 子智能体的 run

日期是**写对象那一刻**的 UTC 日期(recorder 用的 ``finished_at`` 是 run 收尾时的
``datetime.now(UTC)``,不是 ``agent_run.finished_at``;两者相差不过几秒,跨午夜时
可能落在相邻两天 —— 删的一方按 :data:`KEY_DATE_SKEW` 两头都覆盖)。同一对话同一天同一
结果的 run 写同一个键(后写的覆盖先写的)。

子智能体的 run 跑在一个新的 ``thread``(没有 ``thread_meta`` 行)上;B-285 起它的键带上
所属对话(``conversation`` = 最上层那条对话),删对话时按对话的前缀找得到。之前写的子智能
体轨迹是平铺的 ``{thread}.jsonl``,找不到所属对话 —— 留存作业把它们当孤儿删掉(要等 B-286 让
留存作业连上真对象存储)。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final
from uuid import UUID

__all__ = [
    "KEY_DATE_SKEW",
    "MAX_RUN_DAYS",
    "TRAJECTORY_OUTCOMES",
    "TRAJECTORY_PREFIX",
    "ParsedTrajectoryKey",
    "child_trajectory_prefix",
    "parse_trajectory_key",
    "trajectory_days",
    "trajectory_key",
    "union_days",
]

#: Default key prefix (``TrajectoryRecorder.prefix``).
TRAJECTORY_PREFIX: Final = "trajectories"

#: Mini-ADR L-7 —— the four outcome partitions.
TRAJECTORY_OUTCOMES: Final[tuple[str, ...]] = ("success", "failed", "max_steps", "cancelled")

#: How far the object's date can sit from the run's own timestamps: the write
#: happens in a background task after the run's terminal point (bounded by the
#: dispatch timeout, seconds), and replicas' clocks drift. An hour each way is far
#: more than either and still only ever adds the neighbouring day.
KEY_DATE_SKEW: Final = timedelta(hours=1)

#: A run's trajectory date range is clamped to this many days (newest kept). A
#: run lasts minutes (deadline); a range longer than this means bad timestamps.
#: Whatever falls outside is only caught by the retention job's orphan pass, which
#: needs B-286 (the job reaching the real object store) — until then, nothing.
MAX_RUN_DAYS: Final = 8


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def trajectory_key(
    *,
    tenant_id: UUID,
    outcome: str,
    at: datetime,
    thread_id: UUID,
    conversation_id: UUID | None = None,
    prefix: str = TRAJECTORY_PREFIX,
) -> str:
    """The key for one run's trajectory. ``conversation_id`` is set for a
    sub-agent run (its own ``thread_id`` has no ``thread_meta`` row)."""
    if outcome not in TRAJECTORY_OUTCOMES:
        msg = f"invalid outcome {outcome!r}; expected one of {list(TRAJECTORY_OUTCOMES)}"
        raise ValueError(msg)
    ts = _utc(at)
    day = f"{prefix}/{tenant_id}/{outcome}/{ts:%Y}/{ts:%m}/{ts:%d}"
    if conversation_id is not None:
        return f"{day}/{conversation_id}/{thread_id}.jsonl"
    return f"{day}/{thread_id}.jsonl"


def child_trajectory_prefix(
    *,
    tenant_id: UUID,
    outcome: str,
    day: date,
    conversation_id: UUID,
    prefix: str = TRAJECTORY_PREFIX,
) -> str:
    """The prefix under which one conversation's sub-agent trajectories of
    ``day`` / ``outcome`` live (trailing ``/``)."""
    return f"{prefix}/{tenant_id}/{outcome}/{day:%Y}/{day:%m}/{day:%d}/{conversation_id}/"


def trajectory_days(
    *, created_at: datetime, ended_at: datetime, max_days: int = MAX_RUN_DAYS
) -> list[date]:
    """UTC days a run's trajectories (its own and its sub-agents') can be dated:
    ``[created_at - skew, ended_at + skew]``, newest ``max_days`` kept."""
    first = (_utc(created_at) - KEY_DATE_SKEW).date()
    last = (_utc(ended_at) + KEY_DATE_SKEW).date()
    if last < first:
        first, last = last, first
    span = (last - first).days + 1
    days = [last - timedelta(days=i) for i in range(min(span, max_days))]
    return sorted(days)


def union_days(ranges: Iterable[list[date]]) -> list[date]:
    out: set[date] = set()
    for days in ranges:
        out.update(days)
    return sorted(out)


@dataclass(frozen=True)
class ParsedTrajectoryKey:
    tenant_id: UUID
    outcome: str
    day: date
    #: The conversation the object belongs to: the thread itself for a
    #: conversation run, the conversation segment for a sub-agent run.
    conversation_id: UUID
    thread_id: UUID
    #: ``True`` = a sub-agent key (``…/{conversation}/{thread}.jsonl``). A
    #: pre-B-285 sub-agent key is flat and parses as a conversation key whose
    #: ``conversation_id`` never had a ``thread_meta`` row.
    is_child: bool


def parse_trajectory_key(
    key: str, *, prefix: str = TRAJECTORY_PREFIX
) -> ParsedTrajectoryKey | None:
    """Parse a key built by :func:`trajectory_key`; ``None`` for anything else.

    Strict on purpose — the retention job deletes what this recognises, so a key
    that :func:`trajectory_key` could not have produced (upper-case or brace-wrapped
    UUIDs, one-digit months, a 5-digit year …) is never recognised."""
    head = f"{prefix}/"
    if not key.startswith(head) or not key.endswith(".jsonl"):
        return None
    parts = key[len(head) : -len(".jsonl")].split("/")
    if len(parts) not in (6, 7):
        return None
    year, month, day_of_month = parts[2], parts[3], parts[4]
    if not (
        len(year) == 4
        and len(month) == 2
        and len(day_of_month) == 2
        and (year + month + day_of_month).isdigit()
        and (year + month + day_of_month).isascii()
    ):
        return None
    outcome = parts[1]
    if outcome not in TRAJECTORY_OUTCOMES:
        return None
    uuid_parts = [parts[0], *parts[5:]]
    try:
        tenant_id = UUID(parts[0])
        day = date(int(year), int(month), int(day_of_month))
        ids = [UUID(p) for p in parts[5:]]
    except ValueError:
        return None
    if any(str(UUID(p)) != p for p in uuid_parts):
        return None
    if len(ids) == 2:
        return ParsedTrajectoryKey(tenant_id, outcome, day, ids[0], ids[1], is_child=True)
    return ParsedTrajectoryKey(tenant_id, outcome, day, ids[0], ids[0], is_child=False)
