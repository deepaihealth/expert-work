"""P-1 重新生成 / 编辑重发 —— agent_run 两列 + thread_message 一列。

``agent_run.superseded_by_run_id``:这一轮被哪个新 run 取代(NULL = 未被取代)。
``agent_run.regenerated_from_run_id``:这一轮是对哪个旧 run 的重新生成 / 编辑
重发(NULL = 普通轮)。同一逻辑轮的版本链 = 沿 ``regenerated_from_run_id`` 回溯,
墓碑上限按链长计。
``thread_message.superseded_by``:搜索镜像上的同一标记 —— 镜像写入是
``ON CONFLICT (thread_id, seq) DO NOTHING``,永远学不到检查点上后加的标记,
所以 supersede 时显式 UPDATE 这一列。

三列都不加外键:新 run 行在旧轮打标**之后**才 INSERT(锁内顺序写),外键会
逼出错误的写入顺序。

Revision ID: 0153_agent_run_supersede
Revises: 0152_feedback_run_scope
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0153_agent_run_supersede"
down_revision: str | Sequence[str] | None = "0152_feedback_run_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "agent_run", sa.Column("superseded_by_run_id", PG_UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "agent_run", sa.Column("regenerated_from_run_id", PG_UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "thread_message", sa.Column("superseded_by", PG_UUID(as_uuid=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("thread_message", "superseded_by")
    op.drop_column("agent_run", "regenerated_from_run_id")
    op.drop_column("agent_run", "superseded_by_run_id")
