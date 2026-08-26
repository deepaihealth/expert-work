"""``agent_run`` ORM model — Stream J.8 closeout follow-up (Mini-ADR J-41).

Schema mirrors migration 0032_agent_run exactly. Tenant RLS is enforced
at the row level by the migration's policy; the application still
passes ``tenant_id`` so an in-memory backend can match semantics
without a Postgres GUC.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base

_STATUS_VALUES = (
    "('pending', 'queued', 'running', 'success', 'error', 'timeout', 'interrupted', 'paused')"
)
_DISCONNECT_VALUES = "('cancel', 'continue')"


class AgentRunRow(Base):
    """One run's durable lifecycle row — the backing for ``RunManager``."""

    __tablename__ = "agent_run"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    on_disconnect: Mapped[str] = mapped_column(Text, nullable=False)
    is_resume: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stream H.3 PR 2 — Mini-ADR H-9.5. ``current_trace_id_hex()`` is 32
    # chars (16 bytes hex). NULL for legacy rows + auto-triggered runs
    # (scheduler / trigger worker that explicitly pass ``trace_id=None``).
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Stream 9.4 (HA failover) — run-ownership lease. ``claimed_by`` is the
    # executing control-plane instance id; ``lease_until`` is the deadline the
    # owner must renew (via ``heartbeat_at`` touches) or the run is an orphan a
    # peer instance reclaims. All NULL for a run no instance has claimed yet
    # (pending) and for legacy rows.
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stream 9.4 — how many times the orphan sweep has reclaimed this run. The
    # sweep gives up (marks the run errored) past a cap so a run that crashes
    # its owner process every time (OOM / segfault) can't respawn forever.
    reclaim_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Stream 9.5 (distributed run queue) — persisted run input for a ``queued``
    # run, so a ``RunQueueWorker`` on any instance can rebuild ``graph_input``
    # and execute it. NULL for synchronous (SSE) runs and once a queued run is
    # claimed (the input then lives in the checkpoint / event log).
    enqueued_input: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # 产物清单契约(migration 0147)—— run 终局时固化的本 run 产物登记快照
    # ``[{name, kind, version, created_at}]``。NULL = 历史 run / 异常终局无
    # 记录;``[]`` = 零登记(追问轮);产物事后被删不回写(快照语义)。
    artifacts: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    # External-API-v1 P2 block 1-C (migration 0145) — third-party retry
    # dedup. ``idempotency_key`` is the caller's ``Idempotency-Key`` header;
    # ``request_digest`` is a hash of the request body so a *reused* key with
    # a *different* body can be told apart from a genuine retry. Both NULL
    # for runs created without the header (the common case) — the partial
    # unique index below only ever fires on the non-NULL rows.
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_digest: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(f"status IN {_STATUS_VALUES}", name="agent_run_status_valid"),
        CheckConstraint(
            f"on_disconnect IN {_DISCONNECT_VALUES}", name="agent_run_on_disconnect_valid"
        ),
        Index("ix_agent_run_tenant_id", "tenant_id"),
        Index("ix_agent_run_thread_id", "thread_id"),
        Index(
            "ix_agent_run_thread_inflight",
            "thread_id",
            "status",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        # Stream 9.4 — the orphan sweep scans running runs by lease deadline.
        Index(
            "ix_agent_run_lease_sweep",
            "lease_until",
            postgresql_where=text("status = 'running'"),
        ),
        # Stream 9.5 — the run-queue worker scans queued runs by arrival order.
        Index(
            "ix_agent_run_queue_scan",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
        # W1-PR3 Task 1 — the orphan sweep's PENDING-reclaim scan (a replica
        # crashed in the create→RUNNING window, before ever stamping a
        # lease, so ``ix_agent_run_lease_sweep`` never sees the row).
        Index(
            "ix_agent_run_pending_sweep",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # Runs filter-by-user — GET /v1/runs?user_id serves this + newest-first
        # order in one scan; partial (skips system / auto-triggered NULL rows).
        Index(
            "ix_agent_run_tenant_user_created",
            "tenant_id",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        # External-API-v1 P2 block 1-C (migration 0145) — the idempotency
        # dedup key. Partial (only non-NULL keys) so runs without an
        # ``Idempotency-Key`` header never collide; name must match
        # ``0145_agent_run_idempotency``'s ``_INDEX`` verbatim — the SQL
        # store's conflict detection matches on this constraint name.
        Index(
            "uq_agent_run_tenant_idempotency_key",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
