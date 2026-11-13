"""``memory_item`` 加两个可空列 —— B-300 PR 2。

* ``origin`` —— 记忆的来源:``user_stated``(第一次提炼,只看用户输入的文字;或用户本人在
  控制台改写了自己这一条)/ ``assistant_derived``(第二次提炼,看整段对话);空 = 未记录
  (含别人在控制台改写过的)。只加列:不回填、不加索引、不加检查约束 —— 应用层校验,读到
  不认识的值当作空(B-300 设计 §6.1、§6.3)。
* ``embedding_model`` —— 算这一行向量的模型(``provider/model``);空 = 不知道。以后换向量
  模型时靠它分清新旧;重算向量的工具不做(总计划 D11)。

只加可空列,旧镜像不读不写它们,回滚只回镜像、不降级(B-300 设计 §9.2)。``memory_item``
已有的 RLS 策略按行生效,不涉及列,不动。

Revision ID: 0189_b300_memory_origin
Revises: 0188_b300_consolidation_state
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0189_b300_memory_origin"
down_revision: str | Sequence[str] | None = "0188_b300_consolidation_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_COLUMNS = ("origin", "embedding_model")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("memory_item", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("memory_item", name)
