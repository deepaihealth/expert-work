"""B-139 —— 同一会话串行执行,经真实 ``POST /v1/agents/{code}/runs`` 走一遍。

测试栈与 ``test_new_turn_voids_approval`` 共用(真 LangGraph 图 + 内存 store)。
「上一轮还在跑」用一条 **queue 模式、还没被后台队列领走** 的 run 来造:它在库里
是 ``queued`` = 忙,而且什么时候开跑由测试自己决定(调 ``queue_worker().run_once()``),
不用靠睡眠去卡时间。
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from langchain_core.messages import AIMessage

from control_plane.thread_queue import MAX_QUEUED_PER_THREAD
from expert_work.runtime.runs import RunStatus
from tests.test_new_turn_voids_approval import (  # noqa: F401 -- s / script are pytest fixtures
    _AGENT,
    _USER,
    _assert_voided,
    _gated,
    _Stack,
    s,
    script,
)

_SCRIPT = [AIMessage(content="first"), AIMessage(content="leader"), AIMessage(content="follower")]


def _events(body: str) -> list[str]:
    return [line[len("event: ") :] for line in body.splitlines() if line.startswith("event: ")]


async def _stream(stack: _Stack, thread: UUID, text: str) -> httpx.Response:
    return await stack.client.post(
        f"/v1/agents/{_AGENT}/runs",
        json={"user_id": _USER, "input": text, "mode": "stream", "session_id": str(thread)},
    )


@pytest.mark.parametrize("script", [_SCRIPT])
@pytest.mark.asyncio
async def test_a_turn_sent_while_the_session_is_busy_waits_then_runs(
    s: _Stack,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    thread, _, _ = await s.start()
    _, leader, _ = await s.start(thread, mode="queue")

    follower_call = asyncio.create_task(_stream(s, thread, "second question"))
    # 跟随者已经受理、排在 leader 后面;此刻 leader 还没开跑。
    for _ in range(50):
        if len(await s.run_ids(thread)) == 3:
            break
        await asyncio.sleep(0.02)
    assert not follower_call.done()

    assert await s.queue_worker().run_once() == 1  # 只领得到 leader
    await s.settle(leader)
    resp = await asyncio.wait_for(follower_call, timeout=10)

    assert resp.status_code == 200, resp.text
    events = _events(resp.text)
    assert events[0] == "queued"
    assert '"ahead":1' in resp.text.split("\n\n", 1)[0]
    assert events[1] == "metadata"
    assert events[-1] == "end"
    follower = UUID(resp.headers["x-expert-work-run-id"])
    await s.settle(follower)
    leader_row, follower_row = await s.row(leader), await s.row(follower)
    assert leader_row.status is RunStatus.SUCCESS
    assert follower_row.status is RunStatus.SUCCESS
    # 串行:跟随者开跑时 leader 已经结束 —— 历史里两轮按受理顺序排好,一轮都没丢。
    snapshot = await s.snapshot(thread)
    answers = [m.content for m in snapshot.values["messages"] if isinstance(m, AIMessage)]
    assert answers[-2:] == ["leader", "follower"]


@pytest.mark.parametrize("script", [_SCRIPT])
@pytest.mark.asyncio
async def test_an_idle_session_runs_at_once_without_a_queued_frame(
    s: _Stack,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    thread, _, _ = await s.start()
    resp = await _stream(s, thread, "again")
    assert _events(resp.text)[0] == "metadata"


@pytest.mark.asyncio
async def test_the_turn_after_a_full_queue_is_refused(
    s: _Stack,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    thread, _, _ = await s.start()
    for _ in range(MAX_QUEUED_PER_THREAD):
        await s.start(thread, mode="queue")

    resp = await _stream(s, thread, "one too many")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "THREAD_QUEUE_FULL"
    assert len(await s.run_ids(thread)) == 1 + MAX_QUEUED_PER_THREAD


@pytest.mark.parametrize("script", [[AIMessage(content="first"), _gated("A")]])
@pytest.mark.asyncio
async def test_a_queued_turn_voids_the_approval_its_leader_stopped_on(
    s: _Stack,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    """排队期间上一轮停在了审批上(``paused`` 不算忙)—— 轮到这一轮开跑时,出队
    这一侧要把那条审批作废(与后台队列出队同一口径)。受理那一刻还没有审批可废。"""
    thread, _, _ = await s.start()
    _, leader, _ = await s.start(thread, mode="queue")
    follower_call = asyncio.create_task(_stream(s, thread, "second question"))
    for _ in range(50):
        if len(await s.run_ids(thread)) == 3:
            break
        await asyncio.sleep(0.02)

    assert await s.queue_worker().run_once() == 1
    await s.settle(leader)
    assert (await s.row(leader)).status is RunStatus.PAUSED
    resp = await asyncio.wait_for(follower_call, timeout=10)

    follower = UUID(resp.headers["x-expert-work-run-id"])
    await s.settle(follower)
    await _assert_voided(s, leader, by=follower)
    assert s.tool.seen == []
