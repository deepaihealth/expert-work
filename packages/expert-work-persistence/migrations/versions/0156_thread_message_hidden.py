"""thread_message 加 hidden —— 平台脚手架行不进内容搜索(B-73 ①)。

镜像表按设计是**忠实**的:``TranscriptMirrorSweep`` 用 ``include_hidden=True`` 抄下
编排层自己写进检查点的脚手架(B-67 的「本轮输入」段、CM-1 的 ``<recovery-advisory>``),
因为审计要看得见它们。但内容搜索走的是同一张表,于是 B-67 之后每一个 jinja 线程都
会被「清单」「输入」或任意一个变量名命中 —— 假阳性,不泄任何租户文本(那一段里零值),
但运营在对话浏览器里搜东西会被噪音淹。

所以标记留在行上、只在**搜索**那一侧过滤:镜像仍然忠实,审计仍然看得见。

**expand-only,不需要回填**:存量行没有这一列的信息(``MessageTurn`` 此前不带),
默认 ``false`` 对它们是对的 —— 本迁移之前的行里,唯一可能是脚手架的就是 B-67 的
段,而 B-67 还没上过生产。测试环境有这样的行,它们会在 sweep 下次重扫该线程时
被改写成真值(``sync_thread`` 按 ``(thread_id, seq)`` 幂等覆盖)。

Revision ID: 0156_thread_message_hidden
Revises: 0155_sandbox_instance_layout
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0156_thread_message_hidden"
down_revision: str | Sequence[str] | None = "0155_sandbox_instance_layout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "thread_message",
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("thread_message", "hidden")
