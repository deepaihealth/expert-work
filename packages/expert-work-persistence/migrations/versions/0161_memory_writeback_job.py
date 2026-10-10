"""``memory_writeback_job`` —— 长期记忆后台写回的任务表(B-168)。

一轮结束时只落一行指针(会话 + 检查点 + run),控制面的后台 worker 领走后读检查点、
调记忆模型、写记忆,不再挡着本轮的 ``end`` 帧。行里不复制对话内容。

每行至少带一个指针(``checkpoint_id`` 或 ``message_count``,CHECK 约束),处理器绝不去
读整个会话。纯新增一张表,不动存量数据。旧镜像不读它,回滚安全。

索引:

* ``(created_at, id) WHERE status IN ('pending', 'running')`` —— 领取扫描按最早的未完成
  任务找;部分索引只覆盖未完成的行,``done`` / ``failed`` 越积越多也不拖慢领取。
* ``(tenant_id, user_id, status)`` —— 领取时判断同一用户有没有在跑 / 更早的任务;清除用户。
* ``run_id`` —— 控制台按 run 读后台写回结果。
* ``thread_id`` —— 删会话时删它的任务。

租户 RLS 用与 ``thread_message``(0106)同一形式的 ``app.tenant_id`` 策略;``tenant_id``
表都要有策略(``test_rls_policy_coverage``)。只按租户、不按用户:清除用户是管理员在
租户作用域里做的,按用户的策略会让它删不到行。授权走 0121 的默认权限,不用单独 GRANT。

Revision ID: 0161_memory_writeback_job
Revises: 0160_agent_run_thread_busy
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0161_memory_writeback_job"
down_revision: str | Sequence[str] | None = "0160_agent_run_thread_busy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "memory_writeback_job"
_POLICY = "memory_writeback_job_tenant_isolation"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=True),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("written_count", sa.Integer(), nullable=True),
        sa.Column("failed", sa.Boolean(), nullable=True),
        sa.Column("queued_ms", sa.Integer(), nullable=True),
        sa.Column("exec_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="memory_writeback_job_status_enum",
        ),
        sa.CheckConstraint(
            "checkpoint_id IS NOT NULL OR message_count IS NOT NULL",
            name="memory_writeback_job_pointer",
        ),
    )
    op.execute(
        f"""
        CREATE INDEX ix_memory_writeback_job_claim
          ON {_TABLE} (created_at, id)
          WHERE status IN ('pending', 'running')
        """
    )
    op.create_index("ix_memory_writeback_job_user", _TABLE, ["tenant_id", "user_id", "status"])
    op.create_index("ix_memory_writeback_job_run", _TABLE, ["run_id"])
    op.create_index("ix_memory_writeback_job_thread", _TABLE, ["thread_id"])

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON {_TABLE}
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE};")
    op.drop_index("ix_memory_writeback_job_thread", table_name=_TABLE)
    op.drop_index("ix_memory_writeback_job_run", table_name=_TABLE)
    op.drop_index("ix_memory_writeback_job_user", table_name=_TABLE)
    op.drop_index("ix_memory_writeback_job_claim", table_name=_TABLE)
    op.drop_table(_TABLE)
