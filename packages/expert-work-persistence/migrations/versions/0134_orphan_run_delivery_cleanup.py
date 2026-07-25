"""存量孤儿 trigger_run / webhook_delivery 清理 —— 父行已删的历史遗留子行.

Revision ID: 0134_orphan_run_delivery_cleanup
Revises: 0133_mcp_oauth_fk_restrict
Create Date: 2026-07-24

删除接口卫生修复第 3 批 Task 5。``trigger_run.trigger_id``(0033)与
``webhook_delivery.endpoint_id``(0074)历史上都是裸 UUID 列、无 FK ——
删 ``agent_trigger`` / ``webhook_endpoint`` 父行不级联子行,存量孤儿
累积。本迁移一次性 DELETE 指向不存在父行的子行;之后的常规删除由
app 层级联清理(PR3 Task 4 的端点接线,不在本迁移范围内)。

SQL 提为模块级常量:``tests/test_orphan_run_delivery_cleanup.py`` 经
alembic ``ScriptDirectory`` 加载本模块直接执行同一份文本 —— 单一事实
源,不存在测试副本漂移。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0134_orphan_run_delivery_cleanup"
down_revision: str | Sequence[str] | None = "0133_mcp_oauth_fk_restrict"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_DELETE_ORPHAN_TRIGGER_RUN_SQL = """
    DELETE FROM trigger_run tr
    WHERE NOT EXISTS (SELECT 1 FROM agent_trigger t WHERE t.id = tr.trigger_id)
"""

_DELETE_ORPHAN_WEBHOOK_DELIVERY_SQL = """
    DELETE FROM webhook_delivery wd
    WHERE NOT EXISTS (SELECT 1 FROM webhook_endpoint we WHERE we.id = wd.endpoint_id)
"""


def upgrade() -> None:
    op.execute(_DELETE_ORPHAN_TRIGGER_RUN_SQL)
    op.execute(_DELETE_ORPHAN_WEBHOOK_DELIVERY_SQL)


def downgrade() -> None:
    # No-op by design: the deleted rows were orphans — children of parents
    # that no longer exist — and should never have survived their parent's
    # deletion. Irreversibility here is intentional (same posture as 0132).
    pass
