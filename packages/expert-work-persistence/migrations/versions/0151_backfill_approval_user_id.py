"""agent_approval.user_id 存量回填 —— 从 agent_run 取回 run 的所有者。

X-15 ②。审批单落库时 ``user_id`` 被写死成 NULL(`orchestrator/sse.py` 的
``_register_pending_approval``),而超时 sweep 恰恰是从这一列取值,原样喂给
``resolve_approval_decision`` 的 ``caller_user_id`` / ``oauth_user_id``
——「Per-user OAuth MCP pool key — the run's owner」是它自己的注释。于是每一次
被 sweep 续跑的 run 都是无主的,查的是**共享**的 MCP OAuth 池而不是该用户的。
B-20 ③ 把澄清单超时降到 1 小时之后,这条路径从罕见变成常态。

写入侧已在同一批修好。这里只补存量:值一直都在 ``agent_run.user_id`` 上,
两张表通过 ``agent_approval.run_id`` 一一对应,所以回填是无损的、可判定的
——测试环境实测 38 条审批单里 user_id 非空 0 条,而 38 条全部能从 agent_run
取回。

只填 NULL 的行:写入侧修好之后新行自带 owner,回填不该覆盖它们。

downgrade 是 no-op:把刚补上的所有者再抹成 NULL 只会把数据又丢一次,而
「这一列曾经是 NULL」不是任何代码依赖的状态。

Revision ID: 0151_backfill_approval_user_id
Revises: 0150_agent_spec_draft
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0151_backfill_approval_user_id"
down_revision: str | Sequence[str] | None = "0150_agent_spec_draft"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent_approval AS a
           SET user_id = r.user_id
          FROM agent_run AS r
         WHERE r.id = a.run_id
           AND a.user_id IS NULL
           AND r.user_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """No-op — see the module docstring."""
