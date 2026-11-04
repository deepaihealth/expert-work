"""B-269 / B-270 —— ``GET /v1/agents/{agent_code}/runs`` 上的排队与进度字段。

只加信息字段:开始时刻、心跳、排在谁后面、进度计数(不含内容)。测试栈与
``test_external_runs_list`` 共用。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.runtime.runs import RunStatus, RunStore, make_event_record
from tests.test_external_runs_list import _Ctx, ctx  # noqa: F401 -- ctx is a pytest fixture

_URL = "/v1/agents/support-bot/runs"


async def _queue(c: _Ctx, session_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"user_id": "cust-77", "input": "hi", "mode": "queue"}
    if session_id is not None:
        body["session_id"] = session_id
    resp = await c.client.post(_URL, json=body, headers=c.headers)
    assert resp.status_code == 202, resp.text
    data: dict[str, Any] = resp.json()["data"]
    return data


class _Counting:
    """数一个 store 方法被调了几次 —— 列表不许按行 N+1。"""

    def __init__(self, target: Any, name: str) -> None:
        self.calls = 0
        original = getattr(target, name)

        async def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return await original(*args, **kwargs)

        setattr(target, name, counted)


@pytest.mark.asyncio
async def test_run_list_carries_start_heartbeat_queue_and_progress(
    ctx: _Ctx,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    await ctx.seed_agent()
    leader = await _queue(ctx)
    session = leader["thread_id"]
    first, second = await _queue(ctx, session), await _queue(ctx, session)
    finished = await _queue(ctx)
    store: RunStore = ctx.run_store
    started = datetime.now(UTC) - timedelta(minutes=10)
    heartbeat = started + timedelta(minutes=9)
    lead_id = UUID(leader["run_id"])
    await store.set_status(
        run_id=lead_id, tenant_id=ctx.tenant_id, status=RunStatus.RUNNING, updated_at=started
    )
    await store.claim(
        run_id=lead_id,
        tenant_id=ctx.tenant_id,
        claimed_by="replica-a",
        lease_until=heartbeat + timedelta(seconds=30),
        heartbeat_at=heartbeat,
    )
    done_id = UUID(finished["run_id"])
    for status in (RunStatus.RUNNING, RunStatus.SUCCESS):
        await store.set_status(
            run_id=done_id,
            tenant_id=ctx.tenant_id,
            status=status,
            updated_at=started,
            finished_at=started if status is RunStatus.SUCCESS else None,
        )
    tool_call = {"type": "ai", "content": "x", "tool_calls": [{"name": "web_search", "id": "c"}]}
    await ctx.app.state.run_event_store.append_batch(
        [
            make_event_record(
                run_id=lead_id,
                seq=i,
                event_name="updates",
                data=data,
                created_at_ms=int(heartbeat.timestamp() * 1000),
            )
            for i, data in enumerate(
                [
                    {"agent": {"messages": [tool_call], "step_count": 1}},
                    {"tools": {"messages": [{"type": "tool", "name": "web_search"}]}},
                    {"agent": {"messages": [tool_call], "step_count": 2}},
                ]
            )
        ]
    )
    inflight = _Counting(store, "list_inflight_by_threads")
    per_thread = _Counting(store, "list_inflight_by_thread")
    progress = _Counting(ctx.app.state.run_event_store, "progress_for_runs")

    resp = await ctx.client.get(_URL, params={"user_id": "cust-77"}, headers=ctx.headers)

    assert resp.status_code == 200, resp.text
    rows = {r["run_id"]: r for r in resp.json()["data"]["runs"]}
    lead = rows[str(lead_id)]
    assert lead["started_at"] == started.isoformat()
    assert lead["heartbeat_at"] == heartbeat.isoformat()
    assert "queued_behind" not in lead
    p = lead["progress"]
    assert 600 <= p["elapsed_s"] < 660
    assert (p["step"], p["tool_calls"], p["active_workers"]) == (2, 1, 0)
    assert p["current"] == {"node": "agent", "tool": "web_search"}
    assert (
        p["last_event_at"]
        == datetime.fromtimestamp(int(heartbeat.timestamp() * 1000) / 1000, tz=UTC).isoformat()
    )
    assert "plan_done" not in p and "plan_total" not in p

    behind = {"run_id": str(lead_id), "status": "running", "started_at": started.isoformat()}
    for waiting in (first, second):
        row = rows[waiting["run_id"]]
        assert row["queued_behind"] == behind
        assert (row["started_at"], row["heartbeat_at"]) == (None, None)
        assert row["progress"] == {
            "elapsed_s": None,
            "step": 0,
            "tool_calls": 0,
            "active_workers": 0,
            "current": None,
            "last_event_at": None,
        }

    done = rows[str(done_id)]
    assert done["started_at"] == started.isoformat()
    assert not {"heartbeat_at", "queued_behind", "progress"} & set(done)

    # 整页一次排队查询(按租户分批)、一次进度聚合,不按行也不按会话。
    assert (inflight.calls, per_thread.calls, progress.calls) == (1, 0, 1)


@pytest.mark.asyncio
async def test_a_progress_failure_keeps_the_list_and_the_row_fields(
    ctx: _Ctx,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    """进度聚合失败(超时 / 库抖动)只丢 ``progress``,列表照常返回,行上自带的
    ``started_at`` / ``heartbeat_at`` 照给;排队信息另算,不受牵连。"""
    await ctx.seed_agent()
    leader = await _queue(ctx)
    follower = await _queue(ctx, leader["thread_id"])

    async def boom(*_: Any, **__: Any) -> Any:
        raise TimeoutError("statement timeout")

    ctx.app.state.run_event_store.progress_for_runs = boom

    resp = await ctx.client.get(_URL, params={"user_id": "cust-77"}, headers=ctx.headers)

    assert resp.status_code == 200, resp.text
    rows = {r["run_id"]: r for r in resp.json()["data"]["runs"]}
    for row in rows.values():
        assert "progress" not in row
        assert (row["started_at"], row["heartbeat_at"]) == (None, None)
    assert rows[follower["run_id"]]["queued_behind"]["run_id"] == leader["run_id"]


@pytest.mark.asyncio
async def test_a_queue_lookup_failure_keeps_the_list(
    ctx: _Ctx,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    await ctx.seed_agent()
    leader = await _queue(ctx)
    follower = await _queue(ctx, leader["thread_id"])

    async def boom(*_: Any, **__: Any) -> Any:
        raise TimeoutError("statement timeout")

    ctx.run_store.list_inflight_by_threads = boom  # type: ignore[method-assign]

    resp = await ctx.client.get(_URL, params={"user_id": "cust-77"}, headers=ctx.headers)

    assert resp.status_code == 200, resp.text
    row = {r["run_id"]: r for r in resp.json()["data"]["runs"]}[follower["run_id"]]
    assert "queued_behind" not in row
    assert row["progress"]["step"] == 0


@pytest.mark.asyncio
async def test_queued_behind_never_names_another_users_run(
    ctx: _Ctx,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    """对外列表只说自己这个终端用户的 run:挡在前面的是别人的,给 ``null``。"""
    await ctx.seed_agent()
    mine = await _queue(ctx)
    head = await ctx.run_store.get(run_id=UUID(mine["run_id"]), tenant_id=ctx.tenant_id)
    assert head is not None
    stranger = replace(
        head,
        run_id=uuid4(),
        user_id=uuid4(),
        status=RunStatus.RUNNING,
        created_at=head.created_at - timedelta(minutes=1),
        idempotency_key=None,
    )
    await ctx.run_store.create(stranger)

    resp = await ctx.client.get(_URL, params={"user_id": "cust-77"}, headers=ctx.headers)

    row = {r["run_id"]: r for r in resp.json()["data"]["runs"]}[mine["run_id"]]
    assert row["queued_behind"] is None
