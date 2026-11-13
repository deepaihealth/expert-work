"""记忆整理的两张小表 —— B-300 PR A。

* ``memory_consolidation_cursor`` —— 每个租户一行:上一轮整理处理到的最后一个用户。整理按
  用户 id 排序、从它之后取,每处理完一个用户就推进一次(D4),进程重启 / 换副本不会从头再来。
* ``memory_cluster_rejection`` —— 整理模型拒掉的候选簇:按「用户 + 成员 id 排序后的哈希」记,
  只存哈希不存正文;同一组成员下一轮不再问模型,来了新成员哈希就变、重新问。超过 30 天的行
  由整理在同一轮里清掉。

两张表都只按租户做 RLS(与 ``memory_writeback_job``(0161)同一形式):游标是租户级的;被拒
的簇在清除用户时由管理员在租户作用域里删,按用户的策略会让它删不到行。授权走 0121 的默认
权限。纯新增两张表,不动存量数据;旧镜像不读它们,回滚只回镜像、不降级(B-300 设计 §9.2)。

Revision ID: 0188_b300_consolidation_state
Revises: 0187_rate_card_dated_ids
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0188_b300_consolidation_state"
down_revision: str | Sequence[str] | None = "0187_rate_card_dated_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_CURSOR = "memory_consolidation_cursor"
_REJECTION = "memory_cluster_rejection"


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        """
    )


def upgrade() -> None:
    op.create_table(
        _CURSOR,
        sa.Column("tenant_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("last_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    _tenant_policy(_CURSOR)

    op.create_table(
        _REJECTION,
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("member_hash", sa.Text(), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "member_hash"),
    )
    _tenant_policy(_REJECTION)


def downgrade() -> None:
    for table in (_REJECTION, _CURSOR):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};")
        op.drop_table(table)
