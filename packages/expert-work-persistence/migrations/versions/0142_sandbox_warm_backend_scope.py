"""0142 — scope the warm-session unique index to the agent-sandbox backend.

0141 的部分唯一索引不分后端:一个 ``(tenant, user)`` 全局只许一行
IN_USE,docker supervisor 的行与 AgentSandboxClient 的行互相顶死 ——
环境切换 ``sandbox_backend`` 后旧后端残留 warm 行会把新后端同用户的
claim 永久卡死,supervisor 侧 UPDATE 到 IN_USE 也会撞索引。WHERE 加
``image_ref = 'agent-sandbox'``(AgentSandboxClient 写行时的惰性标记,
见 ``expert_work.persistence.sandbox_instance_store.AGENT_SANDBOX_IMAGE_REF``
—— 字面量而非 import,alembic 迁移不依赖应用代码,仓内惯例),索引从此
只 police agent-sandbox 后端自己的行。

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
