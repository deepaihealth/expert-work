"""agent_spec 上的未发布草稿 —— 编辑中的 manifest,不影响任何 run。

保存与发布此前是同一个按钮:点保存 = 立刻对新会话生效。改错了没有回头路,
只能事后回滚。这四列把「存」和「发」分开。

与「某个 Agent 版本」一对一(一个编辑缓冲区),没有生命周期、没有历史、发布
即消失 —— 所以放在同一行而不是单独一张表,单独一张表换来的只是一次 join 和
一份要自己维护的引用完整性。

四列同生同灭:要么全是 NULL(没有草稿),要么全非 NULL。**不加 CHECK 约束**
—— 唯一的写入路径是 store 里的三个方法(存草稿 / 丢弃 / 发布),它们整体
写这四列;一条只为防御 store 自己写错的约束,拦不住真正的错误(写了正确的
四列但内容是错的),却会在将来加第五列时变成一处要同步改的地方。

Revision ID: 0150_agent_spec_draft
Revises: 0149_agent_run_spec_sha
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0150_agent_spec_draft"
down_revision: str | Sequence[str] | None = "0149_agent_run_spec_sha"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("agent_spec", sa.Column("draft_spec_json", JSONB(), nullable=True))
    op.add_column("agent_spec", sa.Column("draft_sha256", sa.CHAR(length=64), nullable=True))
    op.add_column(
        "agent_spec",
        sa.Column("draft_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("agent_spec", sa.Column("draft_updated_by", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_spec", "draft_updated_by")
    op.drop_column("agent_spec", "draft_updated_at")
    op.drop_column("agent_spec", "draft_sha256")
    op.drop_column("agent_spec", "draft_spec_json")
