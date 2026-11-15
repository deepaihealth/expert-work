"""``memory_item`` 加四个可空列 —— B-300 重做 PR M(设计 §9.1)。

* ``evidence_message_id`` / ``evidence_message_index`` —— 证据指针:来源对话(已有的
  ``source_thread_id``)里第几条消息。存 id 是因为它稳;存序号是因为人看得懂、检查点只追加
  不删。对话被删后指针悬空,只是一个编号,不含正文。
* ``occurred_on`` —— 事件日期:某一天发生或观测到的一次性值;计划、偏好、状态为空。
* ``confirmed_at`` —— 用户一方最后一次再说到它的时间;只在用户一方的行上会有值。

``origin`` 列不改:它是没有检查约束的可空文本(0189),新来源值 ``user_confirmed`` 只是应用层
多认一个值。

只加可空列(expand-only):不回填、不加索引、不加检查约束,老行一律不动;旧镜像不读不写它们,
回滚只回镜像、不降级(与 0188 / 0189 同一条理由)。``memory_item`` 已有的 RLS 策略按行生效,
不涉及列,不动。

Revision ID: 0190_b300_memory_evidence
Revises: 0189_b300_memory_origin
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0190_b300_memory_evidence"
down_revision: str | Sequence[str] | None = "0189_b300_memory_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_COLUMNS = (
    ("evidence_message_id", sa.Text()),
    ("evidence_message_index", sa.Integer()),
    ("occurred_on", sa.Date()),
    ("confirmed_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("memory_item", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(_COLUMNS):
        op.drop_column("memory_item", name)
