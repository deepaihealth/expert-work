"""``agent_run`` 按会话找「忙」的 run 的索引(B-139)。

B-139 让同一会话的多轮串行执行:新一轮接单时、后台队列领排队行时,都要回答
「这个会话里有没有更早的、处于 pending / queued / running 的 run」。现有的
``ix_agent_run_thread_inflight`` 只覆盖 ``pending`` / ``running``,不含
``queued``,而排队中的行恰恰是这次要看的。

只加一个部分索引,不动数据。``agent_run`` 量级小(生产近 60 天 110 行),
普通 ``CREATE INDEX`` 的锁时间可以忽略,与 0143 同一做法。

Revision ID: 0160_agent_run_thread_busy
Revises: 0159_skill_usage_viewed
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0160_agent_run_thread_busy"
down_revision: str | Sequence[str] | None = "0159_skill_usage_viewed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_INDEX = "ix_agent_run_thread_busy"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX {_INDEX}
          ON agent_run (thread_id, created_at, id)
          WHERE status IN ('pending', 'queued', 'running')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
