"""P-1「重新生成 / 编辑重发」内核 —— 把会话**最后一轮**标成「已被取代」。

B 方案(spec §3.1):不分叉、不删消息。同一 thread 的检查点里,旧轮的每条
消息按**原 id** 写回一份带 ``expert_work_superseded_by`` 的副本 —— ``add_messages``
reducer 同 id 原地替换,条数 / 下标 / 内容都不变(spike 反证:丢 id 会被追加)。
``plan`` 通道同一次 ``aupdate_state`` 回退到该轮开始前的值。

**定轮不靠消息戳**(ToolMessage 与每轮的 SystemMessage 没有戳),靠检查点
metadata 的 ``run_id``(langchain ``ensure_config`` 把 configurable 标量复制进
metadata;spike 实测 keys = parents / run_id / source / step / tenant_id):
该 run 最早的 checkpoint(``source == "input"``)的 parent 就是「该轮之前」——
它的 ``len(messages)`` 是起始下标,它的 ``plan`` 是回退值。

**但不能用 ``aget_state_history(filter={"run_id": …})`` 去拿**(Task 0 实测,
spec §8-2):它落到 ``AsyncPostgresSaver.alist``,SQL 带两个相关子查询把每个
checkpoint 的 ``checkpoint_blobs`` / ``checkpoint_writes`` 全聚回来,再一次
``fetchall()`` 全量拉回 —— 测试环境最长会话里一个 49 条 checkpoint 的 run 就要
**810-950 ms、52 MB**(每个 checkpoint 都带一整份 messages 通道,随轮数二次
增长;全表最大的 run 有 100 条)。

所以走**两步**:先发两条只取窄列、不碰 blob 的轻 SQL 拿到边界 checkpoint 的
id(各 4-8 ms),再用 ``aget_state(config 带 checkpoint_id=…)`` 取那**两条**
checkpoint(36-89 ms、0.89 MB)。合计实测 40-97 ms。**轻 SQL 走 checkpointer
自己的 psycopg 池,不走 app 的 SQLAlchemy 池** —— ``checkpoints`` 表在
``Settings.checkpointer_dsn`` 那个库里,与 ``db_dsn`` 是两个独立设置,现网指同
一个库但内核不能赌这一点。``InMemorySaver`` 没有表可查(单测 / 内存栈形态),
那条路回退到 ``aget_state_history``:内存 saver 上它是纯 Python 遍历,没有上面
的 blob 放大问题。

**审批链**:审批把一轮切成 PAUSED run + continuation run(``runs.py`` 的审批
续跑,``is_resume=True``、``graph_input=None``),continuation 的最早 checkpoint
是 ``source="loop"``。所以先沿 ``ApprovalRecord.continuation_run_id`` 往前把 run
串成链,链首才是 ``source="input"``;链上每个 run 都记 ``superseded_by_run_id``。

**写入顺序**(spec 说「同一事务」,但 checkpoint 走 psycopg 池、SQL 走
SQLAlchemy,跨不了一个事务):锁内 ① 检查 ② checkpoint 一次 ``aupdate_state``
③ ``agent_run`` ④ ``thread_message`` 镜像 ⑤ ``thread_meta.message_count``。
③ 之后崩掉 = 检查点已标、``agent_run`` 未链:下一次 supersede 看到
``superseded_by_run_id`` 为空或指向不存在的 run,视为**未取代**,重做一遍
(同 id 再替换一次,幂等)。**调用方必须持有 :func:`supersede_thread_lock`**。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.transcript import extract_turns
from expert_work.common.conversation_channel import is_hidden, is_tombstone
from expert_work.common.supersede import mark_superseded, tombstone_message
from expert_work.persistence.approval import ApprovalStore
from expert_work.persistence.thread_message import ThreadMessageStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.runtime.runs import RunInfo, RunStatus, RunStore

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SUPERSEDED_VERSIONS",
    "SUPERSEDE_BUSY_STATUSES",
    "SUPERSEDE_LOCK_CLASSID",
    "SupersedeError",
    "SupersedeResult",
    "TurnLocation",
    "locate_turn",
    "supersede_run",
    "supersede_thread_lock",
]

#: advisory classid。既有取值:workspace_lock 1、mcp_oauth_refresh_lock 2、
#: quality_drift 8615、memory_consolidator 8616、skill_curator 8617、
#: tenant_resource_lock 8618、trigger_delivery / workspace_janitor 8619(历史
#: 上重复占用,键串不同没撞)—— 本模块取 8620,永不共键。
SUPERSEDE_LOCK_CLASSID = 8620

#: 会话里任一 run 处于这些状态 → 409 THREAD_BUSY。与
#: ``api/external_sessions._ACTIVE_RUN_STATUSES`` 同集合(PENDING / QUEUED /
#: RUNNING);``api/plan.py`` 的 ``_WRITE_BLOCKED_STATUSES`` 少了 QUEUED,不能用。
SUPERSEDE_BUSY_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING}
)

#: 同一逻辑轮(沿 ``regenerated_from_run_id`` 回溯的链)最多保留的旧版本数;
#: 超出的最老版本正文置墓碑(spec §3.1-5,拍板 5)。
MAX_SUPERSEDED_VERSIONS = 5


class SupersedeError(Exception):
    """内核拒绝 —— 端点把它渲染成对外信封(code / message / status_code)。"""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@asynccontextmanager
async def supersede_thread_lock(
    session_factory: async_sessionmaker[AsyncSession] | None,
    thread_id: UUID,
) -> AsyncIterator[None]:
    """per-thread 阻塞式 advisory xact lock,照 ``trigger_delivery.delivery_thread_lock``。

    临界区 = 「查状态 → 读 checkpoint → aupdate_state → 三张表 → 建新 run 行」。
    两副本并发对同一轮 supersede:后进的在锁外等,进来后看到
    ``superseded_by_run_id`` 已指向一个**存在的** run → 409。
    ``session_factory=None``(内存栈 / 单测)= no-op。
    """
    if session_factory is None:
        yield
        return
    async with session_factory() as lock_session:
        await lock_session.execute(
            text("SELECT pg_advisory_xact_lock(:cid, hashtext(:k))"),
            {"cid": SUPERSEDE_LOCK_CLASSID, "k": str(thread_id)},
        )
        try:
            yield
        finally:
            # rollback 结束事务 → 释放 xact advisory lock。
            await lock_session.rollback()


@dataclass(frozen=True)
class TurnLocation:
    """一轮在 ``messages`` 通道里的区间 ``[start, end)`` + 轮前的 plan。"""

    start: int
    end: int
    plan_before: Any
    chain_run_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class SupersedeResult:
    location: TurnLocation
    #: ``:regenerate`` 要重放的两条原始消息(该轮的 SystemMessage + 用户
    #: HumanMessage,**打标之前**的原件);``require_replay=False`` 或该轮没有
    #: 这个形状时为 ``None``。
    replay_messages: tuple[BaseMessage, BaseMessage] | None
    superseded_run_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class _Bounds:
    """一条审批链在 checkpoints 里的两个端点(只有 id / parent / source,不含 blob)。"""

    oldest_id: str
    oldest_parent_id: str | None
    oldest_source: str | None
    newest_id: str


#: 只取窄列,**不碰 checkpoint / checkpoint_blobs / checkpoint_writes** —— 这正是
#: ``alist`` 慢的根源。谓词与 Task 0 EXPLAIN 量过的 ``metadata @> '{"run_id":…}'::jsonb``
#: 同为「``checkpoints_pkey`` 索引扫描 + 后置 Filter」,只是这里要一次覆盖整条审批链,
#: 所以写成 ``= ANY(...)``。实测各 4-8 ms。
_BOUNDS_SQL = """
SELECT checkpoint_id, parent_checkpoint_id, metadata->>'source' AS source
FROM checkpoints
WHERE thread_id = %(thread_id)s AND checkpoint_ns = '' AND metadata->>'run_id' = ANY(%(run_ids)s)
ORDER BY checkpoint_id {order}
LIMIT 1
"""


def _checkpoint_pool(graph: Any) -> Any | None:
    """checkpointer **自己的** psycopg 池;内存 saver / 非池形态返回 ``None``。

    ``checkpoints`` 表在 ``Settings.checkpointer_dsn`` 那个库里,与 app 的
    ``db_dsn`` 是两个独立设置 —— 轻查询必须走这个池,不能借
    ``app.state.session_factory``。``TimingCheckpointSaver`` 是我们自己的包装
    (``expert_work/runtime/checkpointer/timing.py``,内层存在 ``_inner``);
    ``AsyncPostgresSaver`` 把池存在 ``.conn``,``make_checkpointer`` 走的是池
    那一支(BUG-18 之后不再是单连接)。
    """
    cp = getattr(graph, "checkpointer", None)
    inner = getattr(cp, "_inner", cp)
    pool = getattr(inner, "conn", None)
    return pool if hasattr(pool, "connection") else None


async def _bounds_sql(pool: Any, thread_id: UUID, run_ids: Sequence[UUID]) -> _Bounds | None:
    params = {"thread_id": str(thread_id), "run_ids": [str(r) for r in run_ids]}
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_BOUNDS_SQL.format(order="ASC"), params)
        oldest = await cur.fetchone()
        if oldest is None:
            return None
        await cur.execute(_BOUNDS_SQL.format(order="DESC"), params)
        newest = await cur.fetchone()
    # 池的 row_factory 是 dict_row(factory.py 的 kwargs),所以按键取。
    return _Bounds(
        oldest_id=oldest["checkpoint_id"],
        oldest_parent_id=oldest["parent_checkpoint_id"],
        oldest_source=oldest["source"],
        newest_id=newest["checkpoint_id"],
    )


async def _bounds_history(
    graph: Any, config: RunnableConfig, run_ids: Sequence[UUID]
) -> _Bounds | None:
    """``InMemorySaver`` 回退路径(单测 / 内存栈)——**只在没有 psycopg 池时走**。

    内存 saver 上 ``aget_state_history`` 是纯 Python 遍历,没有 Postgres 那边
    「每个 checkpoint 都把 blob 聚回来」的放大,所以这里用它没有性能问题。
    生产永远走 :func:`_bounds_sql`。
    """
    oldest: Any = None
    newest: Any = None
    for run_id in run_ids:
        snaps = [s async for s in graph.aget_state_history(config, filter={"run_id": str(run_id)})]
        if not snaps:
            continue
        if newest is None:
            newest = snaps[0]  # 历史最新在前;run_ids[0] 是目标,它的最新就是轮尾
        oldest = snaps[-1]
    if oldest is None or newest is None:
        return None
    return _Bounds(
        oldest_id=oldest.config["configurable"]["checkpoint_id"],
        oldest_parent_id=(oldest.parent_config or {}).get("configurable", {}).get("checkpoint_id"),
        oldest_source=(oldest.metadata or {}).get("source"),
        newest_id=newest.config["configurable"]["checkpoint_id"],
    )


def _at(config: RunnableConfig, checkpoint_id: str) -> RunnableConfig:
    return {"configurable": {**(config.get("configurable") or {}), "checkpoint_id": checkpoint_id}}


async def locate_turn(
    graph: Any,
    config: RunnableConfig,
    *,
    run_ids: Sequence[UUID],
    current_len: int,
    current_plan: Any,
) -> TurnLocation:
    """两步取法:先窄列 SQL 定位该轮的边界 checkpoint,再单条读那两个快照。

    ``run_ids[0]`` 是目标(最新的),之后是它的 PAUSED 前驱(审批链);两条边界
    查询一次覆盖整条链。没有任何检查点(run 在图开始前就失败)→ 空区间
    ``[current_len, current_len)``、plan 不动 —— 只链接 ``agent_run`` 行。

    **只读两条 checkpoint**:链首的 parent(给 ``start`` / ``plan_before``)与链尾
    本身(给 ``end``)。不要为了省一次 ``aget_state`` 用 ``current_len`` 顶替
    ``end`` —— 别的写入缝(投递注入、审批裁定)也会往通道里追加,``end`` 必须
    是这一轮自己最后一个 checkpoint 的长度。
    """
    thread_id = UUID(str((config.get("configurable") or {})["thread_id"]))
    pool = _checkpoint_pool(graph)
    bounds = (
        await _bounds_sql(pool, thread_id, run_ids)
        if pool is not None
        else await _bounds_history(graph, config, run_ids)
    )
    if bounds is None:
        return TurnLocation(
            start=current_len,
            end=current_len,
            plan_before=current_plan,
            chain_run_ids=tuple(run_ids),
        )
    if bounds.oldest_source != "input":
        # 链首不是入口写入 —— 只可能是历史损坏或链没串全;宁可拒绝也别乱标。
        raise SupersedeError(
            "RUN_NOT_LAST", "run boundary could not be established from checkpoint history", 422
        )
    if bounds.oldest_parent_id is None:
        start, plan_before = 0, None
    else:
        parent = await graph.aget_state(_at(config, bounds.oldest_parent_id))
        start = len(parent.values.get("messages") or [])
        plan_before = parent.values.get("plan")
    last = await graph.aget_state(_at(config, bounds.newest_id))
    end = len(last.values.get("messages") or [])
    if not (0 <= start <= end <= current_len):
        raise SupersedeError("RUN_NOT_LAST", "run boundary is outside the current history", 422)
    return TurnLocation(start=start, end=end, plan_before=plan_before, chain_run_ids=tuple(run_ids))


async def _approval_chain(
    target: RunInfo, rows: Sequence[RunInfo], *, approvals: ApprovalStore, tenant_id: UUID
) -> list[UUID]:
    """``[target, PAUSED 前驱, 更早的 PAUSED 前驱, …]`` —— 前驱必须是 PAUSED 且它的
    审批单 ``continuation_run_id`` 正好指向链上后一个 run。"""
    chain = [target.run_id]
    by_id = {r.run_id: r for r in rows}
    ordered = [r.run_id for r in rows]  # list_by_thread 最老在前
    cursor = target.run_id
    while True:
        idx = ordered.index(cursor)
        if idx == 0:
            return chain
        prev = by_id[ordered[idx - 1]]
        if prev.status is not RunStatus.PAUSED:
            return chain
        record = await approvals.get_by_run(run_id=prev.run_id, tenant_id=tenant_id)
        if record is None or record.continuation_run_id != cursor:
            return chain
        chain.append(prev.run_id)
        cursor = prev.run_id


async def _version_chain(target: RunInfo, *, runs: RunStore, tenant_id: UUID) -> list[RunInfo]:
    """同一逻辑轮的版本,新在前:``[target, target.regenerated_from, …]``。"""
    out = [target]
    seen = {target.run_id}
    cursor = target.regenerated_from_run_id
    while cursor is not None and cursor not in seen:
        row = await runs.get(run_id=cursor, tenant_id=tenant_id)
        if row is None:
            break
        out.append(row)
        seen.add(row.run_id)
        cursor = row.regenerated_from_run_id
    return out


def _replay_pair(turn: Sequence[BaseMessage]) -> tuple[BaseMessage, BaseMessage] | None:
    """``build_run_graph_input`` 的形状:[System, 非隐藏 Human, …]。

    不是这个形状就没有可重放的输入(``:regenerate`` 因此 422)。
    """
    if len(turn) < 2:
        return None
    system, human = turn[0], turn[1]
    if not isinstance(system, SystemMessage) or not isinstance(human, HumanMessage):
        return None
    if is_hidden(human):
        return None
    return system, human


async def supersede_run(
    *,
    graph: Any,
    thread_id: UUID,
    tenant_id: UUID,
    target_run_id: UUID,
    new_run_id: UUID,
    runs: RunStore,
    approvals: ApprovalStore,
    thread_messages: ThreadMessageStore,
    threads: ThreadMetaStore,
    require_replay: bool,
) -> SupersedeResult:
    """把 ``target_run_id`` 这一轮标成被 ``new_run_id`` 取代。**调用方持锁**。"""
    rows = await runs.list_by_thread(thread_id=thread_id, tenant_id=tenant_id)
    target = next((r for r in rows if r.run_id == target_run_id), None)
    if target is None:
        raise SupersedeError("RUN_NOT_FOUND", "run not found", 404)
    if target.superseded_by_run_id is not None:
        successor = await runs.get(run_id=target.superseded_by_run_id, tenant_id=tenant_id)
        if successor is not None:
            raise SupersedeError("RUN_ALREADY_SUPERSEDED", "this run was already regenerated", 409)
        # 悬空链接(上一次在建新 run 行之前崩了)→ 视为未取代,重做。
    if target.status is RunStatus.PAUSED:
        raise SupersedeError(
            "RUN_AWAITING_APPROVAL",
            "this run is waiting for an approval decision; decide it first",
            409,
        )
    busy = [r for r in rows if r.status in SUPERSEDE_BUSY_STATUSES]
    if busy:
        raise SupersedeError("THREAD_BUSY", "the session has a run in progress", 409)
    if rows[-1].run_id != target_run_id:
        raise SupersedeError(
            "RUN_NOT_LAST", "only the last run of a session can be regenerated", 422
        )

    config: RunnableConfig = {
        "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
    }
    snapshot = await graph.aget_state(config)
    messages: list[BaseMessage] = list((snapshot.values or {}).get("messages") or [])
    chain = await _approval_chain(target, rows, approvals=approvals, tenant_id=tenant_id)
    location = await locate_turn(
        graph,
        config,
        run_ids=chain,
        current_len=len(messages),
        current_plan=(snapshot.values or {}).get("plan"),
    )
    turn = messages[location.start : location.end]
    replay = _replay_pair(turn)
    if require_replay and replay is None:
        raise SupersedeError(
            "RUN_INPUT_UNAVAILABLE",
            "this run left no reusable input; send :edit with a new input",
            422,
        )

    now = datetime.now(UTC)
    updates: list[BaseMessage] = [
        mark_superseded(m, new_run_id=str(new_run_id), now=now) for m in turn
    ]

    # 留 5 份:版本链(含本次要被取代的 target)超过上限 → 最老的置墓碑。
    versions = await _version_chain(target, runs=runs, tenant_id=tenant_id)
    for old in versions[MAX_SUPERSEDED_VERSIONS:]:
        old_chain = await _approval_chain(old, rows, approvals=approvals, tenant_id=tenant_id)
        old_loc = await locate_turn(
            graph, config, run_ids=old_chain, current_len=len(messages), current_plan=None
        )
        for m in messages[old_loc.start : old_loc.end]:
            if not is_tombstone(m):
                updates.append(tombstone_message(m))

    if updates:
        # 空区间且无墓碑(图开始前就失败的 run)不写 checkpoint —— 没有消息可标,
        # 也不该动 plan。
        await graph.aupdate_state(
            config, {"messages": updates, "plan": location.plan_before}, as_node="agent"
        )

    for run_id in chain:
        await runs.mark_superseded(
            run_id=run_id, tenant_id=tenant_id, superseded_by_run_id=new_run_id
        )
    if turn:
        await thread_messages.mark_superseded(
            thread_id=thread_id,
            tenant_id=tenant_id,
            seq_from=location.start,
            seq_to=location.end,
            superseded_by=new_run_id,
        )
    after = list(((await graph.aget_state(config)).values or {}).get("messages") or [])
    await threads.update_message_count(
        thread_id, len(extract_turns(after, include_hidden=False)), tenant_id=tenant_id
    )
    logger.info(
        "supersede.applied thread=%s target=%s new=%s range=[%d,%d) chain=%d tombstoned=%d",
        thread_id,
        target_run_id,
        new_run_id,
        location.start,
        location.end,
        len(chain),
        len(updates) - len(turn),
    )
    return SupersedeResult(
        location=location,
        replay_messages=replay if require_replay else None,
        superseded_run_ids=tuple(chain),
    )
