"""0162 — ``retention_cleanup_worker`` 能清 ``memory_writeback_job``(B-168)。

retention-cleanup-job 新增一个 pass:收尾(``done`` / ``failed``)满 30 天的记忆后台写回
任务行删掉。0161 建表时只靠 0121 的默认权限给了 ``app_user``,清扫角色拿不到 SELECT /
DELETE 就是 permission denied(同 0131 / 0143 记的教训:清扫 pass 上线要跟着补授权)。
``retention_cleanup_worker`` 是 BYPASSRLS,表上的租户策略不挡它,只补表级 GRANT。

纯授权,不动数据;回滚 = REVOKE。

Revision ID: 0162_writeback_job_retention
Revises: 0161_memory_writeback_job
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0162_writeback_job_retention"
down_revision: str | Sequence[str] | None = "0161_memory_writeback_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "memory_writeback_job"
_RETENTION_ROLE = "retention_cleanup_worker"


def upgrade() -> None:
    op.execute(f"GRANT SELECT, DELETE ON TABLE {_TABLE} TO {_RETENTION_ROLE};")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, DELETE ON TABLE {_TABLE} FROM {_RETENTION_ROLE};")
