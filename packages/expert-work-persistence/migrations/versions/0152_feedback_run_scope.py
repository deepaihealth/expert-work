"""feedback 按 run 归因 + 对外来源 + 改票幂等键;curation_candidate 记「哪一轮被踩 + 原话 + 改票」。

P-2(spec ``2026-09-09-external-feedback-eval-loop-design.md`` §3)。今天的 ``feedback``
只有 ``thread_id``:同一 thread 可无限写重复行,对外端点没有稳定的消息 id 可打分,
三条读路径都稳定的一等 id 只有 ``run_id``。于是:

* ``run_id``(NULL = 迁移前的历史行)、``source``(``console`` / ``external``)、
  ``item_id``(对接方附带的段落标签,只存不 join)、``updated_at``(改票时间,
  NULL = 从没改过)。
* 部分唯一索引 ``(tenant_id, run_id, actor_id) WHERE run_id IS NOT NULL`` —— 「可改票」
  = upsert;历史行 ``run_id`` 为 NULL 不受约束。
* ``curation_candidate`` 四列:审阅员打开候选直接看到哪一轮被踩、用户原话、
  是否后改票、以及**这一踩是员工还是终端用户打的**(``feedback_source``,
  NULL = worker 兜底建的候选,归因不到某一条 feedback)。四列是同一组「反馈
  快照」,同进同出;写入方与展示方在 P-2 PR2 / PR4,本迁移只建列。

``turn_seq`` 保留不动(死字段,另议)。

Revision ID: 0152_feedback_run_scope
Revises: 0151_backfill_approval_user_id
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0152_feedback_run_scope"
down_revision: str | Sequence[str] | None = "0151_backfill_approval_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("feedback", sa.Column("run_id", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "feedback",
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'console'")),
    )
    op.add_column("feedback", sa.Column("item_id", sa.Text(), nullable=True))
    op.add_column("feedback", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "feedback_source_valid", "feedback", "source IN ('console', 'external')"
    )
    op.create_index(
        "feedback_run_actor_uniq",
        "feedback",
        ["tenant_id", "run_id", "actor_id"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )
    op.create_index("ix_feedback_tenant_run", "feedback", ["tenant_id", "run_id"])

    op.add_column(
        "curation_candidate", sa.Column("feedback_run_id", UUID(as_uuid=True), nullable=True)
    )
    op.add_column("curation_candidate", sa.Column("feedback_comment", sa.Text(), nullable=True))
    op.add_column(
        "curation_candidate",
        sa.Column("feedback_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("curation_candidate", sa.Column("feedback_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "candidate_feedback_source_valid",
        "curation_candidate",
        "feedback_source IS NULL OR feedback_source IN ('console', 'external')",
    )
    op.create_index(
        "ix_curation_candidate_feedback_run",
        "curation_candidate",
        ["tenant_id", "feedback_run_id"],
        postgresql_where=sa.text("feedback_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_curation_candidate_feedback_run", table_name="curation_candidate")
    op.drop_constraint("candidate_feedback_source_valid", "curation_candidate", type_="check")
    op.drop_column("curation_candidate", "feedback_source")
    op.drop_column("curation_candidate", "feedback_changed_at")
    op.drop_column("curation_candidate", "feedback_comment")
    op.drop_column("curation_candidate", "feedback_run_id")
    op.drop_index("ix_feedback_tenant_run", table_name="feedback")
    op.drop_index("feedback_run_actor_uniq", table_name="feedback")
    op.drop_constraint("feedback_source_valid", "feedback", type_="check")
    op.drop_column("feedback", "updated_at")
    op.drop_column("feedback", "item_id")
    op.drop_column("feedback", "source")
    op.drop_column("feedback", "run_id")
