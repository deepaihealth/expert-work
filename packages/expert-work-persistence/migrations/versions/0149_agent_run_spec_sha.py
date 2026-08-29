"""agent_run.agent_spec_sha256 —— 这一轮实际执行时用的 manifest 版本。

配置页对 manifest 是**原地编辑**:``thread_meta`` 上记的 ``agent_name`` /
``agent_version`` 编辑前后完全一样,所以在这一列之前,「这条 run 跑的是哪一版
配置」只能拿 run 的 ``created_at`` 去和 ``agent_spec_revision.created_at``
比时间戳猜。值与 ``agent_spec.spec_sha256`` / ``agent_spec_revision.spec_sha256``
同一种规范化形式,可直接等值 join 回那一版的 ``spec_json``。

可空:NULL = 本次迁移之前的历史 run,或 run 在构建成功之前就结束
(配额拒绝 / Agent 被停用 / 构建失败)。都不是「用了空配置」。

不建索引:查询方向永远是「给定 run → 它用了哪版」(主键命中后读一列),
不是「给定某一版 → 哪些 run 用过」。真需要反查时再补。

Revision ID: 0149_agent_run_spec_sha
Revises: 0148_dynamic_worker_caps
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0149_agent_run_spec_sha"
down_revision: str | Sequence[str] | None = "0148_dynamic_worker_caps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column("agent_spec_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_run", "agent_spec_sha256")
