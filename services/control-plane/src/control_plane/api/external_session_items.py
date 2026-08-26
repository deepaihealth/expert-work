"""会话历史条目 —— ``GET /v1/agents/{agent_code}/sessions/{session_id}/items``。

设计见 ``docs/superpowers/specs/2026-08-25-conversation-items-design.md`` §6.1。

第三方要把一段历史会话渲染成与实时对话**视觉一致**的界面,今天要拼三个接口
(消息 / run 列表 / 事件),而且任何一路都不带终端用户自己发的那条消息 —— 它是
执行的输入,只躺在检查点里。这个接口把一段会话直接给成条目列表,形状与实时流
未来发的条目同构,客户端一个 reducer 从头用到尾。

与 ``external_sessions.py`` 分文件:那边已经四百多行,而这里的组装(轮次分页 +
检查点分组 + 辅助信号)是独立的一摊。权限闸、``user_id`` 必填、404 不携带存在性
信息这三条规则两边完全一致。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_session,
    reject_nul_path_params,
)
from control_plane.api._user_scope import get_user_repo
from control_plane.api.external_sessions import _ACTIVE_RUN_STATUSES
from control_plane.runtime import AgentRuntime
from control_plane.transcript import read_messages
from expert_work.common.conversation_channel import message_field
from expert_work.common.conversation_derive import derive_run_items
from expert_work.common.conversation_items import (
    ApprovalItem,
    AuxFrame,
    ConversationItem,
    ToolCallItem,
)
from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.persistence.approval import ApprovalStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol.approval import ApprovalStatus
from expert_work.runtime.runs import RunEventRecord, RunEventStore, RunInfo, RunStore
from expert_work.runtime.runs.event_store import MAX_LIST_LIMIT

logger = logging.getLogger("expert_work.control_plane.api.external_session_items")

#: 一页最多几轮。轮是分页单位(见模块 docstring 引的 spec §6.1),一轮可能展开
#: 成几十条条目,所以上限比按条目分页的接口小得多。
MAX_TURNS_PER_PAGE = 20
DEFAULT_TURNS_PER_PAGE = 5

#: 推导要用的辅助信号帧名。与 ``orchestrator/sse.py`` 的 ``_publish_frame``
#: 调用点同名:``plan``(计划快照)/ ``approval``(等待审批)/ ``error``
#: (这一轮失败了)。其余帧不参与条目推导 —— 消息本身在检查点里,不必读事件。
AUX_EVENT_NAMES: tuple[str, ...] = ("plan", "approval", "error")

#: 子任务帧名。**单列一次查询,不并进** :data:`AUX_EVENT_NAMES` —— ``list``
#: 的 ``limit`` 在名字过滤**之后**截断,而一轮里 worker 帧可以有几百条(每个
#: 子任务每一步一条),合并查询会把排在它们后面的 ``plan`` / ``error`` 挤出
#: 这一页。两次查询各拿满一份预算,谁也挤不掉谁。
WORKER_EVENT_NAMES: tuple[str, ...] = ("worker",)

#: ``worker`` 帧信封的 ``kind``,以及 ``end`` 帧里非成功的两个 ``outcome``。
_WORKER_KINDS = frozenset({"start", "update", "end"})
_WORKER_FAILED_OUTCOMES = frozenset({"max_steps", "cancelled"})


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_run_event_store(request: Request) -> RunEventStore | None:
    store: RunEventStore | None = getattr(request.app.state, "run_event_store", None)
    return store


def _get_approval_store(request: Request) -> ApprovalStore:
    return request.app.state.approval_store  # type: ignore[no-any-return]


def _stamped_run_id(msg: Any) -> str | None:
    """写入侧盖的 ``expert_work_run_id``,没盖就是 ``None``。"""
    stamp = (message_field(msg, "additional_kwargs") or {}).get(STAMP_RUN_ID)
    return stamp if isinstance(stamp, str) else None


def _group_messages_by_run(messages: Sequence[Any]) -> dict[str, list[Any]]:
    """按 ``run_id`` 戳把检查点消息分到各轮,保持原始顺序。

    两条规则决定一条消息归到哪一轮:

    * 盖了戳的消息归戳上那一轮。
    * 工具结果消息(``type == "tool"``)**从来不盖戳** —— 写入侧只给 agent
      节点的助手消息与入口的用户消息盖戳。它归到前一条已归属消息的那一轮,
      因为工具结果结构上必定紧跟发起调用的那条助手消息。

    其余没盖戳的消息(上线前写入的老消息)归不到任何一轮,直接丢弃 —— 编一个
    归属会让它出现在错误的轮次里。
    """
    grouped: dict[str, list[Any]] = {}
    current: str | None = None
    for msg in messages:
        stamped = _stamped_run_id(msg)
        if stamped is not None:
            current = stamped
        elif message_field(msg, "type") != "tool":
            # 老消息:丢弃,并且不让它顶掉「当前轮」—— 后面的工具结果仍然
            # 该跟着它自己那条助手消息走。
            continue
        if current is None:
            continue
        grouped.setdefault(current, []).append(msg)
    return grouped


def _aux_frames(
    records: Sequence[RunEventRecord],
) -> tuple[AuxFrame | None, list[AuxFrame], AuxFrame | None]:
    """一轮的辅助信号帧 → ``(plan, approvals, error)``。

    帧按 ``seq`` 升序进来。``plan`` 与 ``error`` 各取**最后一个**:计划是整份
    快照,一轮里改过几次只有最后一份属于这轮留下的内容;``error`` 同理取最终
    那条。``approval`` 全留,按发生顺序。

    每帧的 ``created_at`` 必须从记录行上取 —— ``plan`` / ``error`` 的时刻不在
    帧内容里,只在服务端记录的时间上。不传的话这两种条目的 ``created_at`` 恒为
    ``None``。
    """
    plan: AuxFrame | None = None
    approvals: list[AuxFrame] = []
    error: AuxFrame | None = None
    for record in records:
        if not isinstance(record.data, Mapping):
            # 帧内容不是对象 —— 推导按键读,喂进去只会炸。历史行里不该有,
            # 但这是从数据库读回来的 JSON,不做假设。
            continue
        frame = AuxFrame(
            data=record.data,
            created_at=record.created_at.isoformat() if record.created_at else None,
        )
        if record.event_name == "plan":
            plan = frame
        elif record.event_name == "approval":
            approvals.append(frame)
        elif record.event_name == "error":
            error = frame
    return plan, approvals, error


def _str_at(data: Mapping[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    return value if isinstance(value, str) else default


def _opt_str_at(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _int_at(data: Mapping[str, Any], key: str) -> int | None:
    """``bool`` 是 ``int`` 的子类,显式挡掉。"""
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _worker_node(frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
    """一个子任务的初始节点 —— 信封字段填好,其余等 start / update / end 补。

    ``parent_worker_id`` / ``parent_tool_call_id`` **不进节点**:它们是拼树用
    的指引,树拼好之后嵌套关系本身就表达了同一件事,留着只会让客户端以为自己
    还得再拼一次。
    """
    return {
        "worker_id": worker_id,
        "label": _str_at(frame, "label"),
        "agent_ref": _str_at(frame, "agent_ref"),
        "depth": _int_at(frame, "depth") or 1,
        # 收不到 ``end`` 帧的子任务停在 ``running``:它异常终止了,或者这一页
        # 的帧被 ``MAX_LIST_LIMIT`` 截断了。两种都不该编一个结局出来。
        "status": "running",
        "task_excerpt": "",
        "role": None,
        "max_steps": None,
        "steps": [],
        "children": [],
        "summary": None,
    }


def _worker_step(frame: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
    """一条 ``kind`` 为 ``update`` 的帧 → 子任务的一步。

    ``messages`` 是**摘要**(正文截 500 字符、参数截 200),不是完整消息 ——
    原样透传,不拿它推导别的条目。
    """
    messages = data.get("messages")
    return {
        "wseq": _int_at(frame, "wseq") or 0,
        "node": _str_at(data, "node", "?"),
        "step_count": _int_at(data, "step_count"),
        "duration_ms": _int_at(data, "_duration_ms") or 0,
        "messages": [dict(m) for m in messages if isinstance(m, Mapping)]
        if isinstance(messages, list)
        else [],
    }


def _worker_trees(records: Sequence[RunEventRecord]) -> dict[str, dict[str, Any]]:
    """一轮的 ``worker`` 帧 → ``{tool_call_id: 子任务树}``。

    分层规则照搬前端那份(``apps/admin-ui/src/api/worker_timeline.ts``),两条
    不能互换:

    * **深度 1 的子任务按 ``parent_tool_call_id`` 挂**到工具调用上。那个值就是
      LangChain 的 ``tool_call_id``,与 ``AIMessage.tool_calls[].id`` 同值。
    * **更深的按 ``parent_worker_id`` 挂树**。孙子任务的 ``parent_tool_call_id``
      指向的是子 run **内部**的一次工具调用,那个 id 从来不出现在父 run 的消息
      里 —— 照它挂等于整棵挂丢。

    防御式:帧形状不对就跳过,父不在场的子任务丢弃,一律不抛 —— 这些是从数据
    库读回来的 JSON,不做形状假设。
    """
    nodes: dict[str, dict[str, Any]] = {}
    parent_worker: dict[str, str | None] = {}
    parent_call: dict[str, str | None] = {}

    for record in records:
        frame = record.data
        if not isinstance(frame, Mapping):
            continue
        worker_id = frame.get("worker_id")
        kind = frame.get("kind")
        if not isinstance(worker_id, str) or kind not in _WORKER_KINDS:
            continue
        node = nodes.get(worker_id)
        if node is None:
            node = nodes[worker_id] = _worker_node(frame, worker_id)
            # 两个父指引只认这个子任务的**第一**帧,与建节点同一时刻定下 ——
            # 同一个子任务的每一帧都原样携带同一份信封。
            parent_worker[worker_id] = _opt_str_at(frame, "parent_worker_id")
            parent_call[worker_id] = _opt_str_at(frame, "parent_tool_call_id")
        payload = frame.get("data")
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        if kind == "start":
            node["task_excerpt"] = _str_at(data, "task_excerpt")
            node["role"] = _opt_str_at(data, "role")
            node["max_steps"] = _int_at(data, "max_steps")
        elif kind == "update":
            node["steps"].append(_worker_step(frame, data))
        else:
            outcome = data.get("outcome")
            node["status"] = outcome if outcome in _WORKER_FAILED_OUTCOMES else "success"
            node["summary"] = {
                "iteration_used": _int_at(data, "iteration_used") or 0,
                "llm_call_count": _int_at(data, "llm_call_count") or 0,
                "wall_clock_ms": _int_at(data, "wall_clock_ms") or 0,
            }

    roots: dict[str, dict[str, Any]] = {}
    for worker_id, node in nodes.items():
        parent_id = parent_worker[worker_id]
        if parent_id is not None:
            parent = nodes.get(parent_id)
            if parent is not None:
                parent["children"].append(node)
            # 父不在场(落库队列把父帧挤掉了 —— ``sse.py`` 满队列时丢最旧那条,
            # 而父的 start 帧比子帧更早)→ **丢弃整棵**,不把孙子任务提成根:
            # 提上来会挂到子 run 内部那个 tool_call 上,那个 id 在父 run 里根本
            # 不存在;万一撞上父 run 里真有的 id,就成了挂到不相干的工具卡上 ——
            # 错挂比不显示坏得多,用户看不出它是错的。
            # 这个 ``continue`` 有测试守着:``test_external_session_items.py::
            # test_orphan_worker_is_dropped_not_promoted_to_a_root``。
            continue
        call_id = parent_call[worker_id]
        if call_id is not None:
            # 一次工具调用结构上只派生一个子任务(``_child_run`` 每次调用建一
            # 个)。瞬时重试重跑同一次调用会留下第二个,以最后那个为准 ——
            # 先前那个已经被放弃了。
            roots[call_id] = node
    return roots


def _with_workers(
    items: Sequence[ConversationItem], trees: Mapping[str, dict[str, Any]]
) -> list[ConversationItem]:
    """给工具调用条目挂上它派生出的子任务树。

    实时路径**不填这个字段**:那边 ``worker`` 仍是独立事件,要等子任务结束才
    能给出工具调用条目的完成事件,时机语义不对。历史没有这个约束 —— 帧都在,
    直接拼好给出去。这是两条路径唯一不完全同构处。
    """
    return [
        replace(item, worker=trees[item.call_id])
        if isinstance(item, ToolCallItem) and item.call_id in trees
        else item
        for item in items
    ]


def _duration_ms(run: RunInfo) -> int | None:
    """这一轮跑了多久;还没结束就是 ``None``。"""
    if run.finished_at is None or run.created_at is None:
        return None
    return int((run.finished_at - run.created_at).total_seconds() * 1000)


def _with_decision(
    items: Sequence[ConversationItem], *, request_id: str | None, decision: str | None
) -> list[ConversationItem]:
    """给这一轮的审批条目回填最终裁定。

    裁定不在任何事件里:``approval`` 事件只在暂停那一刻发一次,人的答复之后
    只更新 ``agent_approval`` 行。一个 run 至多一条审批记录
    (``agent_approval_run_uniq``),所以按 run 取回来再按 ``request_id`` 对上
    就够 —— 用 ``request_id`` 当键去查是不行的,它是同一 thread 内的去重摘要,
    重复的同名审批会撞在一起。
    """
    if decision is None:
        return list(items)
    return [
        replace(item, decision=decision)
        if isinstance(item, ApprovalItem) and item.request_id == request_id
        else item
        for item in items
    ]


def build_external_session_items_router() -> APIRouter:
    """Mount the conversation-items endpoint."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "/{agent_code}/sessions/{session_id}/items",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_session_items(
        agent_code: str,
        session_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        event_store: Annotated[RunEventStore | None, Depends(_get_run_event_store)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        limit: Annotated[int, Query(ge=1, le=MAX_TURNS_PER_PAGE)] = DEFAULT_TURNS_PER_PAGE,
        before: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """一段会话的对话条目,按时间正序;最近 ``limit`` 轮。

        ``mint=False`` 与 404 的规则和 ``/messages`` 完全一致:一个不属于
        ``(user, agent)`` 的会话返回 404 而不是空列表,响应不携带存在性信息。

        分页单位是**轮**(run),不是条目:平台没有条目表,run 才是有 id 有行
        的一等实体,而且用户上滑要的本来就是「更早的几轮」,按条目切会切在一轮
        中间。``before`` 传上一页的 ``first_run_id``。

        正在跑的那一轮不进 ``items``,只给 ``active_run_id`` —— 它的内容客户端
        应当从实时接口拿,两边都给会重复。
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            await load_owned_session(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=user_id,
                session_id=session_id,
                threads=threads,
                users=users,
                mint=False,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        cursor: tuple[datetime, UUID] | None = None
        if before is not None:
            # 游标必须是这段会话自己的 run:404 而不是忽略,否则客户端拿错
            # run_id 翻页会静默拿到「最近几轮」,以为自己翻到了更早的地方。
            anchor = await runs.get(run_id=before, tenant_id=tenant_id)
            if anchor is None or anchor.thread_id != session_id:
                return external_error(ExternalScopeError("RUN_NOT_FOUND", "run not found", 404))
            cursor = (anchor.created_at, anchor.run_id)

        # 多取一行来判断还有没有更早的轮次。keyset 而不是 offset:会话还在继续
        # 时新 run 插在最前,offset 会让客户端把同一轮读两遍。
        page = await runs.list_for_tenant(
            tenant_id=tenant_id,
            thread_ids=[session_id],
            limit=limit + 1,
            before=cursor,
        )
        has_more = len(page) > limit
        page = page[:limit]
        # 分页游标只认「这一页最老的那个 run」,与它是否进 ``items`` 无关 ——
        # 下一页严格更早,不漏也不重。
        first_run_id = str(page[-1].run_id) if page else None

        active = [r for r in page if r.status in _ACTIVE_RUN_STATUSES]
        active_run_id = str(active[0].run_id) if active else None
        active_ids = {r.run_id for r in active}
        # 正序:客户端整页 prepend,与实时流追加在同一个列表末尾。
        turns = [r for r in reversed(page) if r.run_id not in active_ids]

        messages: list[Any] = []
        checkpointer = runtime.durable_checkpointer
        if checkpointer is not None:
            try:
                messages = await read_messages(checkpointer, session_id)
            except Exception:
                # 会话历史读不到时给不完整的结果,不报错 —— 与 ``/messages``
                # 同款降级。轮级信息(状态 / 耗时 / 错误)仍然是准的。
                logger.warning("external_session_items.read_failed", exc_info=True)
        by_run = _group_messages_by_run(messages)

        items: list[dict[str, Any]] = []
        for run in turns:
            key = str(run.run_id)
            plan_frame: AuxFrame | None = None
            approval_frames: list[AuxFrame] = []
            error_frame: AuxFrame | None = None
            worker_trees: dict[str, dict[str, Any]] = {}
            if event_store is not None:
                records = await event_store.list(
                    run_id=run.run_id,
                    limit=MAX_LIST_LIMIT,
                    event_names=AUX_EVENT_NAMES,
                )
                plan_frame, approval_frames, error_frame = _aux_frames(records)
                worker_trees = _worker_trees(
                    await event_store.list(
                        run_id=run.run_id,
                        limit=MAX_LIST_LIMIT,
                        event_names=WORKER_EVENT_NAMES,
                    )
                )
            derived = derive_run_items(
                run_id=key,
                messages=by_run.get(key, []),
                plan=plan_frame,
                approvals=approval_frames,
                error=error_frame,
            )
            if worker_trees:
                derived = _with_workers(derived, worker_trees)
            if approval_frames:
                record = await approvals.get_by_run(run_id=run.run_id, tenant_id=tenant_id)
                if record is not None and record.status is not ApprovalStatus.PENDING:
                    derived = _with_decision(
                        derived, request_id=record.request_id, decision=record.status.value
                    )
            items.extend(item.to_wire() for item in derived)

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "items": items,
                    "runs": [
                        {
                            "run_id": str(run.run_id),
                            "status": run.status.value,
                            "created_at": run.created_at.isoformat() if run.created_at else None,
                            "duration_ms": _duration_ms(run),
                            "error": run.error,
                            # 产物清单契约 —— run 行固化的登记快照;null =
                            # 历史 run 无记录(≠ 零交付,零交付是 [])。
                            "artifacts": run.artifacts,
                        }
                        for run in turns
                    ],
                    "has_more": has_more,
                    "first_run_id": first_run_id,
                    "active_run_id": active_run_id,
                },
                "error": None,
            }
        )

    return router
