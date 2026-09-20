"""``agent_run`` 加 ``completed`` / ``exit_reason`` 两列 —— run 做没做成(B-85 ③)。

``status='success'` 今天的含义是「图跑完了,没抛异常」,**不是「事做成了」**。
2026-09-19 实测抓到的形状:工具失败 → 分类器判不出可重试 → 平台劝模型别重试 →
模型放弃 → 图正常收尾 → ``status=success`` 而零产物。对外 API 的消费者照
``status`` 判成功就会拿到空手。

改 ``status`` 的语义是**对外契约变更**(对接方正在读它),所以走「只加字段」这条路:

* ``exit_reason`` —— run 从哪个出口结束的,由图里知道答案的那一行盖章。
  封闭取值:``text_response`` / ``max_steps`` / ``no_progress`` /
  ``token_budget`` / ``approval_pending`` / ``approval_rejected``。
* ``completed`` —— ``exit_reason == 'text_response'`` 且最后一批工具调用里
  没有未解决的非 transient 失败。

**expand-only,刻意不回填**:``NULL`` = 本改动上线前的老 run,正好对上对外契约那条
「字段缺席 = 无记录,别当成没做成」。回填成 ``false`` 会把一堆本来跑得好好的历史 run
说成没做成;回填成 ``true`` 则会把当年真出过这个问题的那些 run 洗白。两种都是在
没有证据的地方造证据。

Revision ID: 0158_run_completion
Revises: 0157_thread_mirror_resweep
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0158_run_completion"
down_revision: str | Sequence[str] | None = "0157_thread_mirror_resweep"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("completed", sa.Boolean(), nullable=True))
    op.add_column("agent_run", sa.Column("exit_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_run", "exit_reason")
    op.drop_column("agent_run", "completed")
