"""0163 — ``memory_writeback_job``:同一指针只落一行 + 留存清扫用的索引(B-168)。

**唯一索引** ``(run_id, checkpoint_id) NULLS NOT DISTINCT``。写回节点给落任务设了时限,
超时就在本轮里照旧写;那条插入可能在超时之后才提交,节点重试或孤儿复活把写回节点再跑
一遍时,同一个指针不该再多一行任务。store 的 ``enqueue`` 用 ``ON CONFLICT DO NOTHING``
配它,重复时回已有那一行的 id。``checkpoint_id`` 为空的任务(只有 ``message_count``)
也按「相同」算,所以要 ``NULLS NOT DISTINCT``(Postgres 15+)。

**留存索引**:retention-cleanup-job 删收尾满 30 天的行
(``status IN ('done','failed') AND finished_at < …``),部分索引只收收尾的行。

0161 / 0162 已经在测试环境执行过,新的结构只加在这里。建索引前若已有重复指针会失败 ——
写这一条时表里不该有(上线不到一天,且节点只在一处落任务)。非 CONCURRENTLY,同 0143
的理由(迁移跑在事务里);表很小。

Revision ID: 0163_writeback_job_dedup
Revises: 0162_writeback_job_retention
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0163_writeback_job_dedup"
down_revision: str | Sequence[str] | None = "0162_writeback_job_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "memory_writeback_job"
_UNIQUE = "uq_memory_writeback_job_run_checkpoint"
_FINISHED = "ix_memory_writeback_job_finished"


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX {_UNIQUE} ON {_TABLE} (run_id, checkpoint_id) NULLS NOT DISTINCT"
    )
    op.execute(
        f"CREATE INDEX {_FINISHED} ON {_TABLE} (finished_at) WHERE status IN ('done', 'failed')"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_FINISHED}")
    op.execute(f"DROP INDEX IF EXISTS {_UNIQUE}")
