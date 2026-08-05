"""0142 — scope the warm-session unique index to the agent-sandbox backend.

0141 的部分唯一索引不分后端:一个 ``(tenant, user)`` 全局只许一行
IN_USE,docker supervisor 的行与 AgentSandboxClient 的行互相顶死 ——
环境切换 ``sandbox_backend`` 后旧后端残留 warm 行会把新后端同用户的
claim 永久卡死,supervisor 侧 UPDATE 到 IN_USE 也会撞索引。WHERE 加
``image_ref = 'agent-sandbox'``(AgentSandboxClient 写行时的惰性标记,
见 ``expert_work.persistence.sandbox_instance_store.AGENT_SANDBOX_IMAGE_REF``
—— 字面量而非 import,alembic 迁移不依赖应用代码,仓内惯例),索引从此
只 police agent-sandbox 后端自己的行。

部署窗口,两个方向都记录在案,不做代码防御:FORWARD(旧索引仍在、新代码
已经在跑——本迁移落地之前的滚动窗口)的风险已经记在 ``claim_warm`` 的
实现注释里(那期间撞上 docker 行会表现为一次响亮的 could-not-claim
raise,而不是把 docker 的 container id 当 E2B sandbox id 交出去)。这里
补 REVERSE 方向:本迁移先落地、旧代码 pod 仍在滚动更新期间,0142 让
「同一 ``(tenant, user)`` docker 行 + agent 行各一行 ``IN_USE``」第一次
成为合法状态,旧代码里没有 ``image_ref`` 过滤的输家 SELECT(0141 时代
写的那条)会撞上这两行,``.one_or_none()`` 抛 ``MultipleResultsFound``,
那次 ``acquire`` 失败。窗口很窄(需要同时具备:残留的 docker warm 行、
该 ``(tenant, user)`` 并发的第二次 ``acquire``、且仅发生在滚动更新期间)
并且自愈(旧 pod 换成新代码后消失)。

非 CONCURRENTLY 重建(同 0141 的理由):低写表,runbook 记一笔。

Revision ID: 0142_sandbox_warm_backend_scope
Revises: 0141_sandbox_warm_unique
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0142_sandbox_warm_backend_scope"
down_revision: str | Sequence[str] | None = "0141_sandbox_warm_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sandbox_instance_warm_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_sandbox_instance_warm_unique
          ON sandbox_instance (tenant_id, user_id)
          WHERE state = 'IN_USE' AND destroyed_at IS NULL
            AND user_id IS NOT NULL AND image_ref = 'agent-sandbox'
        """
    )


def downgrade() -> None:
    # 还原 0141 原形。注意:若并存期间已产生「同 (tenant, user) 两后端各一行
    # IN_USE」的合法数据,这条 CREATE 会因唯一冲突失败 —— 预期行为,降级前
    # 需人工清掉其中一行。
    op.execute("DROP INDEX IF EXISTS ix_sandbox_instance_warm_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_sandbox_instance_warm_unique
          ON sandbox_instance (tenant_id, user_id)
          WHERE state = 'IN_USE' AND destroyed_at IS NULL AND user_id IS NOT NULL
        """
    )
