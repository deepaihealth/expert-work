"""Abstract :class:`AgentDisableStore` — Stream RT-4 (RT-ADR-16)."""

from __future__ import annotations

import abc
from uuid import UUID

from expert_work.protocol import AgentDisableRecord


class AgentDisableStore(abc.ABC):
    """Persistence for the per-(tenant, agent_name) kill-switch flag."""

    @abc.abstractmethod
    async def get(self, *, tenant_id: UUID, agent_name: str) -> AgentDisableRecord | None:
        """Return the row, or ``None`` when the agent was never disabled.

        A missing row reads as "not disabled" — the enforcement is a
        deliberate admin action, not a default-deny gate (fail-open, same
        rationale as ``tenant_config.status``).
        """

    @abc.abstractmethod
    async def set_disabled(
        self,
        *,
        tenant_id: UUID,
        agent_name: str,
        disabled: bool,
        reason: str | None,
        disabled_by: str | None,
    ) -> AgentDisableRecord:
        """Upsert the kill-switch flag for ``(tenant_id, agent_name)``.

        Insert-or-update: the first write for an agent inserts the row; later
        writes merge. When ``disabled`` is ``True`` the ``disabled_at`` /
        ``disabled_by`` / ``reason`` are stamped; when ``False`` they are
        cleared (a clean re-enable). Returns the resulting record.
        """

    @abc.abstractmethod
    async def list_disabled_names(self, *, tenant_id: UUID) -> set[str]:
        """这个租户当前被禁用的 agent 名字集合。

        阶段 3 (3.1) —— ``GET /v1/agent-catalog`` 要给列表里每个 agent 算
        ``available``,逐个调 :meth:`get` 就是一次 N+1。禁用集很小(kill
        switch 是罕见的管理动作),一次查完整个租户比 N 次点查便宜。

        只返回 ``disabled=True`` 的行。enable 过的 agent 会留下一条
        ``disabled=False`` 的行,它**必须**不在结果里 —— 否则目录端点会把
        一个正常 agent 标成不可用。
        """
