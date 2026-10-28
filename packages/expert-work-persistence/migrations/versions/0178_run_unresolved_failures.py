"""``agent_run`` gains ``unresolved_failures`` — why a finished run was not done (B-226 ⑥).

``completed=false`` with ``exit_reason='text_response'`` says "the model stopped
on its own but a tool failure was never recovered". Partners could not see which
tool. This column keeps the run's final failure ledger as
``[{"tool": <tool name>, "kind": <error class>}]``, at most 20 entries, written in
the same terminal ``set_status`` as ``completed``.

Only the tool name and the error class are kept: the ledger's summary, path,
advice and the call's arguments can carry tenant content and never land here.

**Expand-only, no backfill**: NULL = no record (error / interrupted runs, and
every run before this column); ``[]`` = no unresolved failure. Backfilling ``[]``
would claim "nothing failed" for runs nobody looked at.

Revision ID: 0178_run_unresolved_failures
Revises: 0175_kb_k1_parent_chunks (see the note on ``down_revision``)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0178_run_unresolved_failures"
# Chain note: the merge target is ``0177_kb_retrieval_log`` (PR #1955, not yet on
# main). This branch is based on a main that ends at ``0175_kb_k1_parent_chunks``,
# so it points there for the local migration tests; repoint before merging.
down_revision: str | Sequence[str] | None = "0175_kb_k1_parent_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column("unresolved_failures", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_run", "unresolved_failures")
