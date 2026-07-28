"""perf phase2 PR3 — platform delegation-gate config table.

Adds a single-row (``id == "singleton"``), platform-global, tenant-less table
storing the delegation-gate capacity: ``max_concurrent_delegations``. An
absent row means "not configured" → the platform falls back to its built-in
default.

No RLS policy: tenant-less row, exactly like ``platform_tool_budget_config``
— all access goes through ``bypass_rls_session()``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0137_platform_delegation"
down_revision: str | Sequence[str] | None = "0136_knowledge_chunk_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.create_table(
        "platform_delegation_config",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("max_concurrent_delegations", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("platform_delegation_config")
