"""0174 — 切块新默认 400 / 0 + 文档记切块参数指纹(B-206 K-3)。

* ``knowledge_base.chunk_max_tokens`` / ``chunk_overlap_tokens`` 的库默认改成 400 / 0;
  还停在旧默认 512 / 64 的库一起改过去 —— 控制台建库时把 512 / 64 预填并原样提交,这一对就是
  「没调过」。改过参数的库不动。改的只是参数,已入库的分段要「重新摄取」才按新参数重切。
* ``knowledge_document.chunk_params_hash``:这份文档现有分段是按哪套切块参数切的(切块器版本 +
  上限 + 重叠 + 语义断点开关的哈希,``control_plane.knowledge.chunking.chunk_params_hash``)。
  和库当前参数的哈希对不上 = 旧版本,要重新摄取。旧行为空 = 不知道,也算旧版本。

expand-only:加一列可空列、改默认值与参数,旧镜像不读这一列,参数它照常读。downgrade 删列、
默认值改回 512 / 64;被改过参数的库改不回去(分不出哪些是这里改的)。

Revision ID: 0174_kb_k3_chunking
Revises: 0173_kb_semantic_break_cache
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0174_kb_k3_chunking"
down_revision: str | Sequence[str] | None = "0173_kb_semantic_break_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_OLD_MAX, _OLD_OVERLAP = 512, 64
_NEW_MAX, _NEW_OVERLAP = 400, 0


def upgrade() -> None:
    op.alter_column("knowledge_base", "chunk_max_tokens", server_default=sa.text(str(_NEW_MAX)))
    op.alter_column(
        "knowledge_base", "chunk_overlap_tokens", server_default=sa.text(str(_NEW_OVERLAP))
    )
    op.execute(
        sa.text(
            "UPDATE knowledge_base SET chunk_max_tokens = :new_max, chunk_overlap_tokens = :new_ov"
            " WHERE chunk_max_tokens = :old_max AND chunk_overlap_tokens = :old_ov"
        ).bindparams(new_max=_NEW_MAX, new_ov=_NEW_OVERLAP, old_max=_OLD_MAX, old_ov=_OLD_OVERLAP)
    )
    op.add_column("knowledge_document", sa.Column("chunk_params_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_document", "chunk_params_hash")
    op.alter_column("knowledge_base", "chunk_max_tokens", server_default=sa.text(str(_OLD_MAX)))
    op.alter_column(
        "knowledge_base", "chunk_overlap_tokens", server_default=sa.text(str(_OLD_OVERLAP))
    )
