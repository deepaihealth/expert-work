"""agent_run 幂等键(P2 块 1-C)。

部分唯一索引只覆盖非 NULL 的键 —— 不带 Idempotency-Key 的普通 run 不受影响,
可以建任意多个。唯一索引落在 agent_run 上,所以"占键"与"建 run 行"是同一次
插入,天然原子;不需要单独的键表,也就没有"抢键失败但 run 已建"的孤儿。

不设 TTL:agent_run 行本就为计费/分析永久保留(user_purge 对它是 ANONYMIZE
不是 DELETE),索引只覆盖真带 key 的行,不额外撑存储。

Revision ID: 0145_agent_run_idempotency
Revises: 0144_thread_meta_msg_count
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0145_agent_run_idempotency"
down_revision: str | Sequence[str] | None = "0144_thread_meta_msg_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "agent_run"
_INDEX = "uq_agent_run_tenant_idempotency_key"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("request_digest", sa.Text(), nullable=True))
    op.create_index(
        _INDEX,
        _TABLE,
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "request_digest")
    op.drop_column(_TABLE, "idempotency_key")
