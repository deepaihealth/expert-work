"""B-269 / B-270 —— run 读接口上的信息字段:开始时刻、心跳、排在谁后面、进度。

对外 run 列表与控制台 run 列表 / 详情共用这一处装配,三个出口同一口径:

* ``started_at`` —— 恒给(还没开始执行 / 老 run = ``null``);非终态再给 ``heartbeat_at``。
* ``queued_behind`` —— 只在 ``queued`` 行上:同会话里排在它前面、挡着它的那一轮
  (:func:`control_plane.thread_queue.head_run` 同一条规则),没有 = ``null``。
  整页按租户一次 ``list_inflight_by_threads``,不按行也不按会话查。
* ``progress`` —— 只在非终态行上,整页按租户一次
  :meth:`RunEventStore.progress_for_runs`。只有计数与节点 / 工具名,不含任何内容。

后两项各自读库;读失败(超时 / 库抖动)只让那一项从这一页里缺席并记日志,不连累
列表本身,也不连累另一项和行上自带的时间字段。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from control_plane.thread_queue import head_run, head_summary
from expert_work.runtime.runs import RunEventStore, RunInfo, RunStatus, RunStore
from expert_work.runtime.runs.event_store import RunProgressCounts
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES

logger = logging.getLogger(__name__)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def timing_fields(run: RunInfo) -> dict[str, Any]:
    """``started_at`` 恒给;非终态再给 ``heartbeat_at``(执行者最后一次续约)。"""
    out: dict[str, Any] = {"started_at": _iso(run.started_at)}
    if run.status not in TERMINAL_RUN_STATUSES:
        out["heartbeat_at"] = _iso(run.heartbeat_at)
    return out


def queued_behind_in(
    busy: Sequence[RunInfo], run: RunInfo, *, same_user: bool = False
) -> dict[str, Any] | None:
    """``busy`` = 这一行所在会话的忙 run(按 ``(created_at, id)`` 从早到晚)。

    ``same_user`` —— 对外平面只说调用方这个终端用户自己的 run:挡在前面的是别人的
    就给 ``None``,不把别人的 run id 交出去。"""
    key = (run.created_at, run.run_id)
    head = head_run([b for b in busy if (b.created_at, b.run_id) < key])
    if head is None or (same_user and head.user_id != run.user_id):
        return None
    return head_summary(head)


def _by_tenant(rows: Sequence[RunInfo]) -> dict[UUID, list[RunInfo]]:
    out: dict[UUID, list[RunInfo]] = {}
    for run in rows:
        out.setdefault(run.tenant_id, []).append(run)
    return out


async def queued_behind_by_run(
    store: RunStore, rows: Sequence[RunInfo], *, same_user: bool = False
) -> dict[UUID, dict[str, Any] | None]:
    """每条 ``queued`` 行排在谁后面;整页每个租户读一次库。"""
    out: dict[UUID, dict[str, Any] | None] = {}
    for tenant_id, queued in _by_tenant([r for r in rows if r.status is RunStatus.QUEUED]).items():
        busy = await store.list_inflight_by_threads(
            thread_ids=[r.thread_id for r in queued], tenant_id=tenant_id
        )
        for run in queued:
            out[run.run_id] = queued_behind_in(
                busy.get(run.thread_id, []), run, same_user=same_user
            )
    return out


def progress_dict(
    run: RunInfo, counts: RunProgressCounts | None, *, now: datetime
) -> dict[str, Any]:
    """一个非终态 run 的进度对象;``counts`` 为 ``None`` = 还没有任何落库帧。"""
    elapsed = (
        max(0, int((now - run.started_at).total_seconds())) if run.started_at is not None else None
    )
    out: dict[str, Any] = {
        "elapsed_s": elapsed,
        "step": (counts.step or 0) if counts is not None else 0,
        "tool_calls": counts.tool_calls if counts is not None else 0,
        "active_workers": counts.active_workers if counts is not None else 0,
        "current": (
            {"node": counts.current_node, "tool": counts.current_tool}
            if counts is not None and counts.current_node is not None
            else None
        ),
        "last_event_at": _iso(counts.last_event_at) if counts is not None else None,
    }
    if counts is not None and counts.plan_total is not None:
        out["plan_done"] = counts.plan_done
        out["plan_total"] = counts.plan_total
    return out


async def progress_by_run(
    events: RunEventStore | None, rows: Sequence[RunInfo]
) -> dict[UUID, dict[str, Any]]:
    """非终态行的进度,整页一次聚合。没接事件库的部署照样给(全是零)。"""
    live = [r for r in rows if r.status not in TERMINAL_RUN_STATUSES]
    if not live:
        return {}
    counts: dict[UUID, RunProgressCounts] = {}
    if events is not None:
        for tenant_id, runs in _by_tenant(live).items():
            counts.update(
                await events.progress_for_runs([r.run_id for r in runs], tenant_id=tenant_id)
            )
    now = datetime.now(UTC)
    return {r.run_id: progress_dict(r, counts.get(r.run_id), now=now) for r in live}


async def run_info_fields(
    *,
    runs: RunStore,
    events: RunEventStore | None,
    rows: Sequence[RunInfo],
    same_user: bool = False,
) -> dict[UUID, dict[str, Any]]:
    """每行要并进响应的 B-269 / B-270 字段(键只在该出现时出现)。读库失败的那一项
    在这一页缺席(见模块说明),不抛。"""
    behind: dict[UUID, dict[str, Any] | None] = {}
    try:
        behind = await queued_behind_by_run(runs, rows, same_user=same_user)
    except Exception:
        logger.warning("run_progress.queued_behind_failed", exc_info=True)
    progress: dict[UUID, dict[str, Any]] = {}
    try:
        progress = await progress_by_run(events, rows)
    except Exception:
        logger.warning("run_progress.progress_failed", exc_info=True)
    out: dict[UUID, dict[str, Any]] = {}
    for run in rows:
        fields = timing_fields(run)
        if run.run_id in behind:
            fields["queued_behind"] = behind[run.run_id]
        if run.run_id in progress:
            fields["progress"] = progress[run.run_id]
        out[run.run_id] = fields
    return out
