"""B-269 / B-270 —— 控制台 run 读接口(详情、会话 run 列表、``/v1/runs``)上的
排队与进度字段,与对外 run 列表同一口径(``api/_run_progress.py``)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from control_plane.settings import DEFAULT_DEV_TENANT_ID
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus, make_event_record
from tests.test_runs_api import (  # noqa: F401 -- audit_store / runs_client are pytest fixtures
    _create_session,
    audit_store,
    runs_client,
)

_STARTED = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
_HEARTBEAT = _STARTED + timedelta(minutes=3)


class _Seeded:
    def __init__(self, thread_id: str, leader: UUID, follower: UUID, finished: UUID) -> None:
        self.thread_id, self.leader, self.follower, self.finished = (
            thread_id,
            leader,
            follower,
            finished,
        )


def _info(thread: str, status: RunStatus, *, minutes: int) -> RunInfo:
    at = _STARTED - timedelta(hours=1) + timedelta(minutes=minutes)
    return RunInfo(
        run_id=uuid4(),
        tenant_id=DEFAULT_DEV_TENANT_ID,
        thread_id=UUID(thread),
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=at,
        updated_at=at,
        finished_at=at if status is RunStatus.SUCCESS else None,
        started_at=at if status is RunStatus.SUCCESS else None,
    )


async def _seed(client: AsyncClient) -> _Seeded:
    """一段会话:一轮已完成、一轮在跑(落了两步)、一轮排在它后面。"""
    thread = await _create_session(client)
    app: Any = client._transport.app  # type: ignore[attr-defined]
    store = app.state.run_store
    finished = _info(thread, RunStatus.SUCCESS, minutes=0)
    leader = _info(thread, RunStatus.PENDING, minutes=1)
    follower = _info(thread, RunStatus.QUEUED, minutes=2)
    for row in (finished, leader, follower):
        await store.create(row)
    await store.set_status(
        run_id=leader.run_id,
        tenant_id=DEFAULT_DEV_TENANT_ID,
        status=RunStatus.RUNNING,
        updated_at=_STARTED,
    )
    await store.claim(
        run_id=leader.run_id,
        tenant_id=DEFAULT_DEV_TENANT_ID,
        claimed_by="replica-a",
        lease_until=_HEARTBEAT + timedelta(seconds=30),
        heartbeat_at=_HEARTBEAT,
    )
    await app.state.run_event_store.append_batch(
        [
            make_event_record(
                run_id=leader.run_id,
                seq=0,
                event_name="updates",
                data={"agent": {"messages": [], "step_count": 1}},
                created_at_ms=int(_HEARTBEAT.timestamp() * 1000),
            ),
            make_event_record(
                run_id=leader.run_id,
                seq=1,
                event_name="worker",
                data={"worker_id": "w1", "kind": "start"},
                created_at_ms=int(_HEARTBEAT.timestamp() * 1000),
            ),
            make_event_record(
                run_id=leader.run_id,
                seq=2,
                event_name="plan",
                data={"goal": "g", "steps": [{"id": "1", "status": "completed"}, {"id": "2"}]},
                created_at_ms=int(_HEARTBEAT.timestamp() * 1000),
            ),
        ]
    )
    return _Seeded(thread, leader.run_id, follower.run_id, finished.run_id)


def _assert_leader(row: dict[str, Any]) -> None:
    assert row["started_at"] == _STARTED.isoformat()
    assert row["heartbeat_at"] == _HEARTBEAT.isoformat()
    assert "queued_behind" not in row
    progress = row["progress"]
    assert progress["elapsed_s"] > 0
    assert (progress["step"], progress["tool_calls"], progress["active_workers"]) == (1, 0, 1)
    assert progress["current"] == {"node": "agent", "tool": None}
    assert (progress["plan_done"], progress["plan_total"]) == (1, 2)


def _assert_follower(row: dict[str, Any], seeded: _Seeded) -> None:
    assert row["queued_behind"] == {
        "run_id": str(seeded.leader),
        "status": "running",
        "started_at": _STARTED.isoformat(),
    }
    assert row["progress"]["elapsed_s"] is None


def _assert_finished(row: dict[str, Any]) -> None:
    assert row["started_at"] is not None
    assert not {"heartbeat_at", "queued_behind", "progress"} & set(row)


@pytest.mark.asyncio
async def test_run_detail_carries_queue_and_progress(
    runs_client: AsyncClient,  # noqa: F811 -- pytest fixture injection
) -> None:
    seeded = await _seed(runs_client)
    base = f"/v1/sessions/{seeded.thread_id}/runs"

    leader = (await runs_client.get(f"{base}/{seeded.leader}")).json()
    follower = (await runs_client.get(f"{base}/{seeded.follower}")).json()
    finished = (await runs_client.get(f"{base}/{seeded.finished}")).json()

    _assert_leader(leader)
    _assert_follower(follower, seeded)
    _assert_finished(finished)


@pytest.mark.asyncio
async def test_thread_run_list_carries_queue_and_progress(
    runs_client: AsyncClient,  # noqa: F811 -- pytest fixture injection
) -> None:
    seeded = await _seed(runs_client)

    resp = await runs_client.get(f"/v1/sessions/{seeded.thread_id}/runs")

    rows = {r["run_id"]: r for r in resp.json()["data"]["runs"]}
    _assert_leader(rows[str(seeded.leader)])
    _assert_follower(rows[str(seeded.follower)], seeded)
    _assert_finished(rows[str(seeded.finished)])


@pytest.mark.asyncio
async def test_cross_thread_run_list_carries_queue_and_progress(
    runs_client: AsyncClient,  # noqa: F811 -- pytest fixture injection
) -> None:
    seeded = await _seed(runs_client)

    resp = await runs_client.get("/v1/runs")

    rows = {r["run_id"]: r for r in resp.json()["data"]["items"]}
    _assert_leader(rows[str(seeded.leader)])
    _assert_follower(rows[str(seeded.follower)], seeded)
    _assert_finished(rows[str(seeded.finished)])
