"""``thread_meta`` gains ``workspace_layout`` — which sandbox view a conversation uses (B-236).

Until B-236 every conversation of one user + agent shared one sandbox ``/workspace``
(the agent root), so two conversations running at once overwrote each other's
fixed-name files. From B-236 on a conversation's ``/workspace`` is its own directory
(``agents/<key>/threads/<conversation>/work``) and the agent root is ``/agent``.

* ``'agent_root'`` — the pre-B-236 view, kept for every conversation that already
  exists at deploy: a revision asked in an open conversation must still find that
  conversation's earlier outputs where they were written.
* ``'conversation'`` — the per-conversation view, for every thread created after.

The column is added with default ``'agent_root'`` (so every existing row reads it,
metadata-only in PostgreSQL 11+, no table rewrite) and **the default stays
``'agent_root'``**. New code writes ``'conversation'`` explicitly at the one place a
thread row is created (``SqlThreadMetaStore.create``). That keeps a rolling deploy
consistent: a thread created by an old pod (which never names the column, and whose
exec still lays out the old view) reads ``'agent_root'``, so a new pod that later runs
it keeps the view its files were written in. A CHECK keeps the value to these two.

**Expand-only.** Old code never names the column and ignores the value; an image
rollback needs no downgrade (the downgrade drops it).

Revision ID: 0181_thread_workspace_layout
Revises: 0180_artifact_version_snapshot
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0181_thread_workspace_layout"
down_revision: str | Sequence[str] | None = "0180_artifact_version_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_CHECK = "thread_meta_workspace_layout_values"


def upgrade() -> None:
    op.add_column(
        "thread_meta",
        sa.Column("workspace_layout", sa.Text(), nullable=False, server_default="agent_root"),
    )
    op.create_check_constraint(
        _CHECK, "thread_meta", "workspace_layout IN ('conversation', 'agent_root')"
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, "thread_meta", type_="check")
    op.drop_column("thread_meta", "workspace_layout")
