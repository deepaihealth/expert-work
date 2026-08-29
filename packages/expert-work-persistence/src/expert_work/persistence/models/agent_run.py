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
    # and execute it. Synchronous (SSE) runs never write it.
    #
    # 两处容易读错(2026-08-29 排查 token 记账时踩到):
    #
    # 1. 这一列每次 INSERT 都显式写,SSE 的 run 写进来的 Python ``None`` 落成
    #    的是 **JSON ``null``**,不是 SQL NULL —— SQLAlchemy JSON 类型的默认
    #    行为(``none_as_null`` 默认 False)。所以
    #    ``WHERE enqueued_input IS NOT NULL`` 对 SSE 的 run 也判真,
    #    **拿它分不出 queue 和 stream**(2026-08-29 测试环境 450 行:SQL NULL
    #    0 个,JSON null 374 个)。Python 侧没这个坑:JSON ``null`` 解出来就是
    #    ``None``。下面的 ``artifacts`` **不是**这样 —— 它只在 run 终局
    #    UPDATE,没写过的行是真 SQL NULL(同一批 450 行:291 个 SQL NULL,
    #    JSON null 0 个),所以那一列的「NULL vs []」用 SQL 也筛得对。
    # 2. 领取(``claim_queued``)和 run 终局都**不清空**这一列,全仓没有
    #    任何地方清。值会一直留着,里面是用户发进来的原始输入。目前没有
    #    代码依赖它被清空(worker 领取时一次性读走,orphan sweep 续跑走的是
    #    checkpoint),而同样的内容本来也留在 ``run_event`` 和 checkpoint 里,
    #    所以只清这一列换不来什么 —— 但做数据留存时别按「跑完就没了」算。
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
    # 这一轮实际执行时用的 manifest 内容哈希(sha256 十六进制,64 字符;与
    # ``agent_spec.spec_sha256`` / ``agent_spec_revision.spec_sha256`` 同一种
    # 规范化形式,可直接等值 join 回那一版的 ``spec_json``)。
    #
    # 为什么非有这一列不可:配置页对 manifest 是**原地编辑**,``thread_meta``
    # 上记的 ``agent_name`` / ``agent_version`` 编辑前后一模一样。没有它,
    # 「这条 run 跑的是哪一版配置」只能拿 run 的 ``created_at`` 去和
    # ``agent_spec_revision.created_at`` 比时间戳猜。
    #
    # 写入时机是**构建 agent 之后**(``run_trace.bind_exec_spec``),不是建行时:
    # 排队的 run 建行时还没构建过,而执行时读到的可能已经是编辑后的版本 ——
    # 记建行时的值就会记错。审批续跑是新 run 行(``is_resume=True``),所以
    # 一行始终只对应一次构建,不存在一行两版。
    #
    # NULL 的两种含义:这一列上线前的历史 run;以及 run 在构建成功之前就结束
    # (配额拒绝 / Agent 被停用 / 构建失败)。都**不是**「用了空配置」。
    agent_spec_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
