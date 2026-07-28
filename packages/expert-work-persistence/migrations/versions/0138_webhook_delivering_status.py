"""W1-PR1 — admit the ``delivering`` webhook_delivery status (CAS claim).

Widens the ``webhook_delivery.status`` CHECK to include the new in-flight
state written by ``WebhookDeliveryStore.claim_ready`` — the atomic CAS
claim (``UPDATE ... FOR UPDATE SKIP LOCKED ... RETURNING``) that lets
multiple delivery-worker replicas share the queue without double-POSTing
the same event (multi-replica readiness, deploy-w1). The constraint
mirrors ``_DELIVERY_STATUS_VALUES`` in the ORM model.

Also widens the ``ix_webhook_delivery_ready`` partial index predicate to
include ``delivering`` (final terminal review, I-1): ``claim_ready`` added a
third OR arm — ``status = 'delivering' AND updated_at <= stale_before`` — to
reclaim stuck in-flight rows, but that arm isn't implied by the index's old
``status IN ('pending', 'retrying')`` predicate. An OR clause where one arm
falls outside the partial index's predicate makes Postgres discard the
index for the whole query and fall back to a sequential scan — on every
15s sweep, from every worker replica, against a table with no retention
cleanup (unbounded growth). Mirrors ``ix_webhook_delivery_ready`` in the
ORM model.

Revision ID: 0138_webhook_delivering_status
Revises: 0137_platform_delegation
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0138_webhook_delivering_status"
down_revision: str | Sequence[str] | None = "0137_platform_delegation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "webhook_delivery"
_CONSTRAINT = "webhook_delivery_status_valid"
_OLD = "('pending', 'delivered', 'failed', 'retrying', 'dead_letter')"
_NEW = "('pending', 'delivering', 'delivered', 'failed', 'retrying', 'dead_letter')"

_INDEX = "ix_webhook_delivery_ready"
_OLD_INDEX_WHERE = "status IN ('pending', 'retrying')"
_NEW_INDEX_WHERE = "status IN ('pending', 'retrying', 'delivering')"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT};")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK (status IN {_NEW});")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX};")
    op.execute(f"CREATE INDEX {_INDEX} ON {_TABLE} (next_retry_at) WHERE {_NEW_INDEX_WHERE};")


def downgrade() -> None:
    # Static identifiers only (module constants) — no user input reaches this.
    op.execute(f"DROP INDEX IF EXISTS {_INDEX};")
    op.execute(f"CREATE INDEX {_INDEX} ON {_TABLE} (next_retry_at) WHERE {_OLD_INDEX_WHERE};")
    # ``delivering`` is transient (an in-flight claim, not a terminal DLQ
    # state) — reset to ``pending`` rather than deleting the row, so a
    # downgrade never silently drops a queued delivery.
    op.execute(f"UPDATE {_TABLE} SET status = 'pending' WHERE status = 'delivering';")  # noqa: S608
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT};")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK (status IN {_OLD});")
