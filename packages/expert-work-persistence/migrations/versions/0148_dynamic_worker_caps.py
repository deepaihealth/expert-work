"""弹性 worker 预算 — platform dynamic-worker hard-cap columns.

Adds the hard-cap tier (``cap_max_concurrent`` / ``cap_max_per_run`` /
``cap_max_iterations``) to the single-row ``platform_dynamic_worker_config``
table. The existing three columns become the platform *default* tier (what an
agent gets when its manifest does not ask); the new columns are the ceiling a
per-agent ``dynamic_workers.max_*`` request is clamped to at run time.

Server defaults (10 / 64 / 128) backfill the (at most one) existing row with
the platform's recommended caps; new writes always set the columns explicitly.

Revision id ``0148_dynamic_worker_caps`` = 24 chars (within the 32-char
alembic ``version_num`` ceiling per [memory:alembic-revision-id-32-chars]).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0148_dynamic_worker_caps"
down_revision: str | Sequence[str] | None = "0147_agent_run_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "platform_dynamic_worker_config",
        sa.Column("cap_max_concurrent", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "platform_dynamic_worker_config",
        sa.Column("cap_max_per_run", sa.Integer(), nullable=False, server_default="64"),
    )
    op.add_column(
        "platform_dynamic_worker_config",
        sa.Column("cap_max_iterations", sa.Integer(), nullable=False, server_default="128"),
    )


def downgrade() -> None:
    op.drop_column("platform_dynamic_worker_config", "cap_max_iterations")
    op.drop_column("platform_dynamic_worker_config", "cap_max_per_run")
    op.drop_column("platform_dynamic_worker_config", "cap_max_concurrent")
