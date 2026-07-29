"""W1-PR3 Task 1 — PENDING orphan-sweep partial index.

Backs ``RunStore.list_stale_pending`` (the create→RUNNING transient-window
recovery scan — an instance crash inside that window before ever stamping
an ownership lease leaves a PENDING row permanently stuck, which also
silently blocks ``control_plane.api.plan``'s external-plan-write gate
forever) with a partial index on ``created_at`` scoped to
``status='pending'``. Mirrors ``ix_agent_run_queue_scan``'s partial-index
shape exactly (migration 0082_agent_run_queue) — same table, same single
``created_at`` column, just a different status predicate.

Revision ID: 0139_agent_run_pending_sweep
Revises: 0138_webhook_delivering_status
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0139_agent_run_pending_sweep"
down_revision: str | Sequence[str] | None = "0138_webhook_delivering_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_INDEX = "ix_agent_run_pending_sweep"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "agent_run",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="agent_run")
