"""0141 — sandbox warm-session unique index (波 1 Task 7).

一个 ``(tenant, user)`` 同时只该有一个活跃热沙箱。并发 acquire 靠这个
部分唯一索引定单赢家:两路都 ``INSERT ... ON CONFLICT DO NOTHING
RETURNING``,拿到行的建沙箱,没拿到的读赢家的 ``container_id`` 直接
connect。与 triggers program 的"端点建唯一行、两路 CAS 同行单赢家"
同一配方。

非 CONCURRENTLY 建索引(仓内惯例):``sandbox_instance`` 是低写表
(每次 acquire 一行),进部署 runbook 记一笔即可。

Revision ID: 0141_sandbox_warm_unique
Revises: 0140_token_usage_audit_grant
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0141_sandbox_warm_unique"
down_revision: str | Sequence[str] | None = "0140_token_usage_audit_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    # ``user_id IS NOT NULL`` is required: user_id is nullable (an ephemeral
    # sandbox with no persistent workspace), and multiple NULL rows never
    # conflict in Postgres but semantically this index should not police them.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_sandbox_instance_warm_unique
          ON sandbox_instance (tenant_id, user_id)
          WHERE state = 'IN_USE' AND destroyed_at IS NULL AND user_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sandbox_instance_warm_unique")
