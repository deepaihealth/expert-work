"""curation_candidate 悬空回退 + eval_dataset FK RESTRICT —— dataset 删除 DB 级兜底.

Revision ID: 0135_curation_candidate_fk
Revises: 0134_orphan_run_delivery_cleanup
Create Date: 2026-07-24

删除接口卫生修复第 3 批 Task 8。``curation_candidate.eval_dataset_id``(0034)
历史上是裸 UUID 列、无 FK —— 删 ``eval_dataset`` 父行后 PROMOTED candidate
永久卡死:指针悬空、无回退路径。app 层已改为先回退再删(PR3 Task 7),本
迁移做三件事:

1. 存量悬空 PROMOTED(指向不存在的 dataset)一次性回退 ``pending`` + 清指针
   —— 样本回待审池,可再 promote;
2. 清其余悬空指针(非 PROMOTED 行的 ``eval_dataset_id`` 指向不存在的 dataset);
3. 补 ``ON DELETE RESTRICT`` FK 作 DB 级兜底 —— 防未来代码绕过 app 闸再造悬空。

SQL 提为模块级常量:``tests/test_curation_candidate_fk.py`` 经 alembic
``ScriptDirectory`` 加载本模块直接执行同一份文本 —— 单一事实源,不存在测试
副本漂移。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0135_curation_candidate_fk"
down_revision: str | Sequence[str] | None = "0134_orphan_run_delivery_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_FK_NAME = "fk_curation_candidate_eval_dataset"

_REVERT_DANGLING_PROMOTED_SQL = """
    UPDATE curation_candidate c SET status = 'pending', eval_dataset_id = NULL
    WHERE c.status = 'promoted' AND c.eval_dataset_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM eval_dataset d WHERE d.id = c.eval_dataset_id)
"""

_CLEAR_DANGLING_POINTER_SQL = """
    UPDATE curation_candidate c SET eval_dataset_id = NULL
    WHERE c.eval_dataset_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM eval_dataset d WHERE d.id = c.eval_dataset_id)
"""


def upgrade() -> None:
    op.execute(_REVERT_DANGLING_PROMOTED_SQL)
    op.execute(_CLEAR_DANGLING_POINTER_SQL)
    op.create_foreign_key(
        _FK_NAME,
        "curation_candidate",
        "eval_dataset",
        ["eval_dataset_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "curation_candidate", type_="foreignkey")
    # The data fix is irreversible by design (same posture as 0132/0134):
    # the reverted candidates pointed at datasets that no longer exist —
    # there is no dangling state worth restoring.
