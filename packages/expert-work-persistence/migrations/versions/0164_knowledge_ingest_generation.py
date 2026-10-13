"""0164 — ``knowledge_document.ingest_generation``:重传 / 重入库的代际围栏(B-175 L2-PR2)。

同名重传会把文档重置为 ``pending`` 并重新入库;原来旧任务(上一次上传的 worker)
晚一步写回时,会把旧分段盖在新内容上并把状态置 ``ready``。现在每次重置把
``ingest_generation`` 加一,worker 认领时带走当时的代际,写回(分段替换 / 状态 /
终态失败)都加谓词 ``status = 'processing' AND ingest_generation = :gen``,不匹配就不写。

**pending 格子**:``pending_content`` / ``pending_content_sha256``。``content`` 只放「正在生效的
那版」原件;重传的新版先进 pending,新版入库成功时(带围栏的 READY 写回)才提升成 ``content``,
失败则原件、分段都还是旧版,文档继续可检索,可重试新版也可丢弃(``discard_pending``)。

expand-only:新列带默认值 / 可空,旧镜像不读;回滚不需要 downgrade。

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
    op.add_column(
        "knowledge_document", sa.Column("pending_content", sa.LargeBinary(), nullable=True)
    )
    op.add_column(
        "knowledge_document", sa.Column("pending_content_sha256", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("knowledge_document", "pending_content_sha256")
    op.drop_column("knowledge_document", "pending_content")
    op.drop_column("knowledge_document", "ingest_generation")
