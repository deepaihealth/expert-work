"""``WorkspaceQuotaService`` —— 沙箱迁移波 3 (spec § 3) 用户工作区存储配额闸。

实现 :class:`orchestrator.tools.agent_sandbox.WorkspaceQuotaGate` Protocol,
供 Task 5(app.py post-assign)把它挂到 ``AgentSandboxClient.quota_gate``。
两条谓词刻意不同,不要统一:

* 闸 A(:meth:`check`,acquire 前拦)—— ``size_bytes >= limit``。
* 闸 B(:meth:`check_upload`,上传前拦)—— ``size_bytes + incoming > limit``。

前者是"已经到顶就不许再进沙箱干活",后者是"这次写入会不会把你推过线"——
同一上限,两个不同时刻问的不同问题,`==` 边界的取舍天然相反(A 在原地不
动也该被拦;B 恰好写满不该被拦)。

配额上限来自 ``tenant_quota`` 表的 ``WORKSPACE_BYTES_PER_USER`` 维度
(:meth:`effective_limit`),没配的租户回落到平台默认
:data:`~expert_work.protocol.quota.DEFAULT_WORKSPACE_BYTES_PER_USER`。
实际字节数来自 :meth:`refresh` 对 NAS 目录的 ``du`` 重算(lstat,不追踪软
链接——避免用户在自己的工作区里链到宿主机大文件把配额数字吹上天),写入路
径用 :meth:`note_written` 做增量记账,:meth:`refresh_soon` 是防抖过的
fire-and-forget 触发入口。``user_workspace.size_limit_bytes`` 列不读——那
是 supervisor(波 1/2 冻结路径)专用的字段,本闸完全绕开它。

## capacity 与 lifecycle 分离(评审裁决)

:meth:`~expert_work.persistence.workspace.base.UserWorkspaceStore.resolve`
的契约明确写着"soft-deleted 行仍会被返回——软删的强制执行是调用方的事,
调用方必须自己检查 ``deleted_at``"。本 service 故意**不**对所有四个方法一
律加这层检查,而是按 capacity(闸)/lifecycle(生命周期)分开裁决:

* :meth:`check` / :meth:`check_upload` —— **不**做软删闸。这两个是纯容量
  判断;lifecycle 语义已经分别由 acquire 路径自己的 soft-delete marker 闸
  (``AgentSandboxClient``)和上传路径自己的闸各自拥有,配额闸重复判一次
  只会制造两个真相源。
* :meth:`refresh` / :meth:`note_written` —— **会**检查:命中
  ``ws.deleted_at is not None`` 时直接 return,不碰这一行。这两个是写路
  径(重算 / 记账),待归档的行不该再被这两条路径写。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from expert_work.persistence.quota import TenantQuotaStore
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.protocol.quota import DEFAULT_WORKSPACE_BYTES_PER_USER, QuotaDimension
from orchestrator.tools.nas_workspace_store import workspace_user_root
from orchestrator.tools.sandbox import WorkspaceQuotaExceededError

logger = logging.getLogger(__name__)


class WorkspaceQuotaService:
    """租户维度取上限(时效窗口)/ 两谓词检查 / du 重算,60s 防抖。"""

    def __init__(
        self,
        *,
        user_workspaces: UserWorkspaceStore,
        tenant_quotas: TenantQuotaStore,
        workspace_root: str,
        debounce_s: float = 60.0,
    ) -> None:
        self._user_workspaces = user_workspaces
        self._tenant_quotas = tenant_quotas
        self._workspace_root = workspace_root
        self._debounce_s = debounce_s
        self._last_refresh: dict[tuple[UUID, UUID], float] = {}

    async def effective_limit(self, *, tenant_id: UUID) -> int:
        """当前对 ``tenant_id`` 生效的每用户工作区字节上限。

        只认 ``WORKSPACE_BYTES_PER_USER`` 维度、``scope`` 为空、时间窗口
        活跃(``effective_from <= now``,``effective_until`` 为 ``None`` 或
        ``> now``)的行;没有命中就回落平台默认。
        """
        now = datetime.now(UTC)
        rows = await self._tenant_quotas.list_by_tenant(tenant_id=tenant_id)
        for row in rows:
            if (
                row.dimension is QuotaDimension.WORKSPACE_BYTES_PER_USER
                and not row.scope
                and row.effective_from <= now
                and (row.effective_until is None or row.effective_until > now)
            ):
                return row.limit_value
        return DEFAULT_WORKSPACE_BYTES_PER_USER

    async def check(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """闸 A —— ``size_bytes >= limit`` 拦(acquire 前调用)。"""
        ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        if ws.size_bytes >= await self.effective_limit(tenant_id=tenant_id):
            raise WorkspaceQuotaExceededError("user workspace is over its storage quota")

    async def check_upload(self, *, tenant_id: UUID, user_id: UUID, incoming_bytes: int) -> None:
        """闸 B —— ``size_bytes + incoming > limit`` 拦(上传前调用)。"""
        ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        if ws.size_bytes + incoming_bytes > await self.effective_limit(tenant_id=tenant_id):
            raise WorkspaceQuotaExceededError("user workspace is over its storage quota")

    async def note_written(self, *, tenant_id: UUID, user_id: UUID, delta_bytes: int) -> None:
        """写入成功后的增量记账 —— resolve(建行) + ``add_size``。

        软删(``deleted_at is not None``)行直接 return,不记账——见模块
        docstring"capacity 与 lifecycle 分离"一节。
        """
        ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        if ws.deleted_at is not None:
            return
        await self._user_workspaces.add_size(workspace_id=ws.id, delta_bytes=delta_bytes)

    async def refresh(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """对该用户的 NAS 目录做一次 ``du`` 重算,写回 ``update_size``。

        无防抖 —— janitor 扫描 / 测试直调走这里;高频路径走
        :meth:`refresh_soon`。软删(``deleted_at is not None``)行直接
        return,不重算——见模块 docstring"capacity 与 lifecycle 分离"一节。
        """
        ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        if ws.deleted_at is not None:
            return
        root = workspace_user_root(self._workspace_root, tenant_id, user_id)

        def _du() -> int:
            total = 0
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for entry in it:
                            try:
                                st = entry.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            else:
                                total += st.st_size
                except FileNotFoundError:
                    # 根目录本身不存在(用户还没写过任何东西)—— 0。非 root
                    # 子目录在遍历中途消失(NAS 并发写删的常态)—— 跳过它,
                    # 继续扫栈上其余兄弟目录;绝不能提前 return,否则会静默
                    # 丢掉还没扫的目录,少算配额(评审 Important-1)。
                    if d == root:
                        return 0
                    continue
                except OSError:
                    continue
            return total

        size = await asyncio.to_thread(_du)
        await self._user_workspaces.update_size(workspace_id=ws.id, size_bytes=size)

    def refresh_soon(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """防抖(60s per (tenant_id, user_id))+ fire-and-forget 触发 :meth:`refresh`。

        同步方法,内部自行调度 ``create_task``;异常在任务体内吞掉只
        log —— 调用方(release 路径)不该因为重算失败而受影响。
        """
        key = (tenant_id, user_id)
        now = time.monotonic()
        last = self._last_refresh.get(key)
        if last is not None and now - last < self._debounce_s:
            return
        self._last_refresh[key] = now

        async def _run() -> None:
            try:
                await self.refresh(tenant_id=tenant_id, user_id=user_id)
            except Exception:  # spec § 3.2:失败吞掉,janitor 兜底
                logger.exception(
                    "workspace_quota.refresh_failed tenant=%s user=%s", tenant_id, user_id
                )

        asyncio.get_running_loop().create_task(_run())
