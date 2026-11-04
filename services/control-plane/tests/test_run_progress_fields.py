"""B-269 / B-270 —— ``api/_run_progress`` 的纯函数口径。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from control_plane.api._run_progress import progress_dict, queued_behind_in
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.runs.event_store import RunProgressCounts

_T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _run(status: RunStatus, minutes: int, started_at: datetime | None = None) -> RunInfo:
    at = _T0 + timedelta(minutes=minutes)
    return RunInfo(
        run_id=uuid4(),
        tenant_id=uuid4(),
        thread_id=uuid4(),
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=at,
        updated_at=at,
        finished_at=None,
        started_at=started_at,
    )


def test_the_first_waiting_turn_is_behind_nobody() -> None:
    """排在最前的排队行前面没人(上一轮刚结束、还没被领走),不是「排在自己后面」。"""
    first, second = _run(RunStatus.QUEUED, 0), _run(RunStatus.QUEUED, 1)
    busy = [first, second]
    assert queued_behind_in(busy, first) is None
    assert queued_behind_in(busy, second) == {
        "run_id": str(first.run_id),
        "status": "queued",
        "started_at": None,
    }


def test_same_user_only_hides_another_users_head() -> None:
    head = _run(RunStatus.RUNNING, 0, started_at=_T0)
    waiting = replace(_run(RunStatus.QUEUED, 1), user_id=uuid4())
    busy = [head, waiting]
    assert queued_behind_in(busy, waiting)["run_id"] == str(head.run_id)  # type: ignore[index]
    assert queued_behind_in(busy, waiting, same_user=True) is None
    mine = replace(waiting, user_id=head.user_id)
    assert queued_behind_in([head, mine], mine, same_user=True) is not None


def test_a_later_running_turn_does_not_block_an_earlier_waiting_one() -> None:
    waiting, later = _run(RunStatus.QUEUED, 0), _run(RunStatus.RUNNING, 1, started_at=_T0)
    assert queued_behind_in([waiting, later], waiting) is None


def test_progress_without_frames_is_zeros_and_elapsed_counts_from_start() -> None:
    run = _run(RunStatus.RUNNING, 0, started_at=_T0)
    now = _T0 + timedelta(seconds=90)
    assert progress_dict(run, None, now=now) == {
        "elapsed_s": 90,
        "step": 0,
        "tool_calls": 0,
        "active_workers": 0,
        "current": None,
        "last_event_at": None,
    }


def test_progress_plan_fields_only_with_a_plan() -> None:
    run = _run(RunStatus.RUNNING, 0, started_at=_T0)
    counts = RunProgressCounts(
        step=4,
        tool_calls=7,
        active_workers=2,
        current_node="tools",
        current_tool=None,
        last_event_at=_T0,
        plan_done=None,
        plan_total=None,
    )
    out = progress_dict(run, counts, now=_T0)
    assert "plan_done" not in out and "plan_total" not in out
    assert out["current"] == {"node": "tools", "tool": None}
    planned = progress_dict(run, replace(counts, plan_done=0, plan_total=3), now=_T0)
    assert (planned["plan_done"], planned["plan_total"]) == (0, 3)
