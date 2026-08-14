"""thread_meta.message_count —— 对外会话列表的消息条数(P2 块 2)。

口径是**第三方可见**的条数(``include_hidden=False``),由 run 终局重算后写入。
刻意不给 server_default:存量行留 NULL 表示"尚未算过",与"真的是空会话"(0)
区分开 —— 填成 0 会让前端把没算过的会话显示成空会话。

Revision ID: 0144_thread_meta_msg_count
Revises: 0143_egress_audit_scan_idx
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0144_thread_meta_msg_count"
down_revision: str | Sequence[str] | None = "0143_egress_audit_scan_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "thread_meta"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("message_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "message_count")
