"""sandbox_instance 加 layout —— 热会话的沙箱内布局版本(B-60)。

spec ``docs/superpowers/specs/2026-09-14-workspace-exec-mount-namespace-design.md`` §4.6。
``'user-root'`` = NAS 直接挂 ``/workspace`` 的旧布局(本迁移之前建的每一行都是它);
``'agent-ns'`` = NAS 挂 ``/mnt/workspace``、每次 exec 在自己的命名空间里把 agent
目录 bind 成 ``/workspace``。``AgentSandboxClient.acquire`` 拿到与本进程不同布局的热
会话就销毁重建(``destroy_reason='layout_mismatch'``)—— 所以这是 expand-only,
不需要回填:默认值就是存量行的真实布局。

Revision ID: 0155_sandbox_instance_layout
Revises: 0154_artifact_agent_key
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0155_sandbox_instance_layout"
down_revision: str | Sequence[str] | None = "0154_artifact_agent_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "sandbox_instance",
        sa.Column("layout", sa.Text(), nullable=False, server_default="user-root"),
    )


def downgrade() -> None:
    op.drop_column("sandbox_instance", "layout")
