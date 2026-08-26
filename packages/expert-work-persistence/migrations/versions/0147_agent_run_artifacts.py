"""agent_run.artifacts —— run 产物清单快照(产物清单契约)。

run 终局时与状态同一次 UPDATE 写入本 run 登记过的产物清单
(``[{name, kind, version, created_at}]``)。快照语义:产物事后被
软删/同名覆盖不回写历史清单 —— 清单答「产出过什么」,不答「现在还有
什么」。可空三态:NULL = 迁移前的历史 run(或异常终局没走到固化,如
orphan sweep 收割的 run)无记录;``[]`` = 新 run 零登记(追问轮的显式
零交付);非空 = 交付轮。

Revision ID: 0147_agent_run_artifacts
Revises: 0146_user_upload
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0147_agent_run_artifacts"
down_revision: str | Sequence[str] | None = "0146_user_upload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column("artifacts", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_run", "artifacts")
