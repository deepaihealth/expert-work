"""B-293 —— 撤掉一个对外请求自己刚建、随后被拒的会话。

``POST /v1/agents/{code}/runs`` 与 ``POST /v1/agents/{code}/uploads`` 不带 ``session_id`` 时先建会话
(``agents._resolve_session``),再做可能拒绝的检查。被拒却不撤的话,对接方每重试一次,这位用户
的会话列表里就多一条空会话(测试环境 2026-11-06:``files[]`` 引用不存在的上传,404,空会话留下)。

新会话在建好到被撤之间是看得见的(``GET .../sessions`` 不滤空会话),别的请求可以拿它的 id
起 run / 传文件。所以撤之前在会话的接单锁(``supersede_thread_lock``,``spawn_run`` 建 run 行用的
同一把)里确认没人在用:没有 run 行、没有图片行、登记行全是本请求自己的;删会话行的那条语句
自己再带一遍这几个条件。``spawn_run`` 在同一把锁里重读会话行,会话已经被撤就回 404。

撤的顺序同会话彻底删除:会话行先删(之后剩下的目录由留存作业的孤儿目录扫描兜),再删本请求
登记的行(id 从没给过对接方,直接删,不留墓碑),最后删会话自己的目录(``agents/<key>/threads/
<id>/``;老视图的会话落 agent 共用的 ``uploads/``,同 purge 不删)—— 有时限:落盘出错那条路上
沙箱本来就在出错,拒绝回包不能再被它拖住。目录删掉了,就把本请求落盘时记过的
``note_written(+字节)`` 减回去(``add_size`` 以 0 为下限);目录没删掉时字节还在盘上,不减。

不撤:终端用户行与 ``agent_instance`` 绑定 —— 两者都按 ``user_id`` 幂等(重试不会多),同一用户的
并发请求共用它们,``POST .../sessions`` 首次接触同样会铸。

尽力而为:每步失败只记异常类型,不改变给对接方的回包。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Request

from control_plane.api._workspace_shared import workspace_agent_path
from control_plane.supersede import supersede_thread_lock
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.protocol.agent_key import sanitize_agent_key
from expert_work.runtime.runs import RunStore
from orchestrator.context.workspace_projection import thread_projection_prefix
from orchestrator.tools import WorkspaceStore
from orchestrator.tools.registry import WorkspaceLayout

logger = logging.getLogger("expert_work.control_plane.new_session_undo")

#: 删新会话目录最多等这么久;没删掉的由留存作业的孤儿目录扫描兜(会话行已经先删了)。
_DISCARD_DIR_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class NewSessionUndo:
    """撤会话要用到的存储(都取自 ``app.state``,与端点依赖注入的是同一批对象)。"""

    threads: ThreadMetaStore
    runs: RunStore | None
    uploads: UserUploadStore
    images: ImageUploadStore
    workspace_store: WorkspaceStore | None
    #: 工作区用量(``WorkspaceQuotaService``);非 NAS 部署没接线为 ``None``。
    quota_service: Any
    #: 接单锁用的库连接;内存栈为 ``None``(锁是空操作)。
    session_factory: Any

    @classmethod
    def from_request(cls, request: Request) -> NewSessionUndo:
        state = request.app.state
        return cls(
            threads=state.thread_meta_repo,
            runs=getattr(state, "run_store", None),
            uploads=state.user_upload_store,
            images=state.image_upload_store,
            workspace_store=getattr(state, "workspace_store", None),
            quota_service=getattr(state, "workspace_quota_service", None),
            session_factory=getattr(state, "session_factory", None),
        )


async def discard_new_session(
    undo: NewSessionUndo,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    end_user_id: UUID,
    agent_code: str,
    workspace_layout: WorkspaceLayout,
    landed_files: bool,
    own_upload_ids: Collection[UUID] = (),
    landed_bytes: int = 0,
) -> None:
    """撤掉本请求建的会话 ``thread_id``;有人在用就不撤。

    ``landed_files`` = 这个请求可能往新会话里落过东西(run 的 ``files[]`` 跨会话复制;上传落盘)。
    ``own_upload_ids`` = 本请求在新会话里登记的行(跨会话复制),不算「别人在用」;它们的
    ``size_bytes`` 就是复制时记过的字节。``landed_bytes`` = 另外落下、没有登记行的字节(上传
    落盘之后登记出错)。
    """
    try:
        async with supersede_thread_lock(undo.session_factory, thread_id):
            removed = not await _in_use(
                undo, tenant_id=tenant_id, thread_id=thread_id, own_upload_ids=own_upload_ids
            ) and await undo.threads.delete_unused(
                thread_id, tenant_id=tenant_id, own_upload_ids=own_upload_ids
            )
    except Exception as exc:
        logger.warning("new_session.discard_failed error=%s", type(exc).__name__)
        return
    if not removed:
        logger.warning("new_session.discard_skipped_in_use")
        return
    if landed_files:
        await _remove_landed(
            undo,
            tenant_id=tenant_id,
            thread_id=thread_id,
            end_user_id=end_user_id,
            agent_code=agent_code,
            workspace_layout=workspace_layout,
            own_upload_ids=own_upload_ids,
            landed_bytes=landed_bytes,
        )


async def _in_use(
    undo: NewSessionUndo, *, tenant_id: UUID, thread_id: UUID, own_upload_ids: Collection[UUID]
) -> bool:
    if undo.runs is not None and await undo.runs.list_by_thread(
        thread_id=thread_id, tenant_id=tenant_id
    ):
        return True
    if await undo.images.list_for_threads(tenant_id=tenant_id, thread_ids=[thread_id]):
        return True
    rows = await undo.uploads.count_for_threads(tenant_id=tenant_id, thread_ids=[thread_id])
    return rows > len(set(own_upload_ids))


async def _remove_landed(
    undo: NewSessionUndo,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    end_user_id: UUID,
    agent_code: str,
    workspace_layout: WorkspaceLayout,
    own_upload_ids: Collection[UUID],
    landed_bytes: int,
) -> None:
    own = set(own_upload_ids)
    try:
        rows = await undo.uploads.list_documents_for_thread(
            tenant_id=tenant_id, thread_id=thread_id
        )
        landed_bytes += sum(row.size_bytes for row in rows if row.id in own)
        await undo.uploads.delete_for_threads(tenant_id=tenant_id, thread_ids=[thread_id])
    except Exception as exc:
        logger.warning("new_session.discard_uploads_failed error=%s", type(exc).__name__)
    if workspace_layout == "conversation" and undo.workspace_store is not None:
        path = workspace_agent_path(
            thread_projection_prefix(thread_id).rstrip("/"),
            agent_key=sanitize_agent_key(agent_code),
        )
        try:
            await asyncio.wait_for(
                undo.workspace_store.delete_tree(
                    tenant_id=tenant_id, user_id=end_user_id, path=path
                ),
                timeout=_DISCARD_DIR_TIMEOUT_S,
            )
        except Exception as exc:  # 含超时:会话行已经没了,留存作业的孤儿目录扫描兜
            logger.warning("new_session.discard_dir_failed error=%s", type(exc).__name__)
            return
        if landed_bytes > 0 and undo.quota_service is not None:
            try:
                await undo.quota_service.note_written(
                    tenant_id=tenant_id, user_id=end_user_id, delta_bytes=-landed_bytes
                )
            except Exception as exc:
                logger.warning("new_session.discard_quota_failed error=%s", type(exc).__name__)
