"""0164 — ``knowledge_document.ingest_generation``:重传 / 重入库的代际围栏(B-175 L2-PR2)。

同名重传会把文档重置为 ``pending`` 并重新入库;原来旧任务(上一次上传的 worker)
晚一步写回时,会把旧分段盖在新内容上并把状态置 ``ready``。现在每次重置把
``ingest_generation`` 加一,worker 认领时带走当时的代际,写回(分段替换 / 状态 /
终态失败)都加谓词 ``status = 'processing' AND ingest_generation = :gen``,不匹配就不写。

expand-only:带默认值 0,旧镜像不读这一列;回滚不需要 downgrade。

Revision ID: 0164_knowledge_ingest_generation
Revises: 0163_writeback_job_dedup
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0164_knowledge_ingest_generation"
down_revision: str | Sequence[str] | None = "0163_writeback_job_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "knowledge_document",
        sa.Column(
            "ingest_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_document", "ingest_generation")
