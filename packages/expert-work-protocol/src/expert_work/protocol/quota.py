"""Quota engine cross-service schemas — Stream C.5.

Authoritative source: subsystems/16-quota-rate-limit § 3.3.

The control plane's :class:`control_plane.quota.QuotaService` consumes
:class:`CheckRequest` / :class:`ReserveRequest` / :class:`CommitRequest`
and returns :class:`CheckResult` / :class:`ReserveResult`. The same
schemas are echoed over the internal HTTP surface
(``POST /v1/quota/{check,reserve,commit,release/{id}}``) and across the
in-process Python boundary so callers can swap implementations freely.

M0 deliberately omits the ``model`` dimension from rate-limit decisions
(it lives on :class:`ReserveRequest` for audit / metric labelling but
isn't part of the bucket key). M1 adds it as a real dimension; the
field shape is forward-compatible.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CheckRequest",
    "CheckResult",
    "CommitRequest",
    "QuotaDimension",
    "QuotaPurpose",
    "ReservationState",
    "ReserveRequest",
    "ReserveResult",
    "TenantBudgetRecord",
    "TenantQuotaPatch",
    "TenantQuotaRecord",
    "TokenReservationRecord",
]


class QuotaDimension(StrEnum):
    """Rate-limit dimensions supported in M0.

    Subsystems/16 § 9 reserves ``MODEL`` for M1; landing it here keeps
    the enum forward-compatible without changing call sites.
    """

    QPS = "qps"
    TOKENS_PER_DAY = "tokens_per_day"
    SANDBOXES = "sandboxes"
    MONTHLY_TOKEN_BUDGET = "monthly_token_budget"  # noqa: S105 (dimension name, not a secret)
    # Mini-ADR J-30 (J.6.补强-1) — image upload count over a rolling 30-day
    # window. Bucket capacity = limit_value, refill_rate = limit / (30 *
    # 86400) — a slow drip approximating the rolling window (same shape
    # as ``TOKENS_PER_DAY`` / ``QPS``, just a longer window). ``cost=1``
    # per upload.
    IMAGE_UPLOAD_COUNT_30D = "image_upload_count_30d"
    # Mini-ADR J-30 — current total image storage bytes per tenant.
    # Bucket capacity = limit_value (bytes), refill_rate = 0 — a sticky
    # ceiling. ``cost = file_size`` per upload. (Image lifecycle
    # deletion → bytes refund is the J.6.补强-3 / Mini-ADR J-32 scope,
    # not landed yet.)
    IMAGE_STORAGE_BYTES = "image_storage_bytes"
    # Mini-ADR J-25 (J.9-step2) — artifact download count over a rolling
    # 30-day window. Same shape as ``IMAGE_UPLOAD_COUNT_30D``: bucket
    # capacity = limit_value, refill_rate = limit / (30 * 86400) — slow
    # drip approximating the rolling window. ``cost=1`` per download.
    ARTIFACT_DOWNLOAD_COUNT_30D = "artifact_download_count_30d"
    # Mini-ADR J-25 — total artifact storage bytes per user (sticky
    # ceiling like ``IMAGE_STORAGE_BYTES``). Wired at the save-artifact
    # tool layer is a follow-up step (orchestrator → quota plumbing);
    # for M0 the J.15 ``user_workspace`` volume quota (Mini-ADR J-29) is
    # the underlying storage cap, and this dimension is reserved for
    # future per-artifact charging.
    ARTIFACT_STORAGE_BYTES = "artifact_storage_bytes"
    # 沙箱迁移波 3 (spec § 3.1) — 每用户工作区字节黏性上限。存储型配置值:
    # 由 workspace 配额闸直接读 limit_value,**不接** bucket ladder
    # (redis_quota.py / in_memory.py 对它必须保持惰性;有回归测试钉住)。
    WORKSPACE_BYTES_PER_USER = "workspace_bytes_per_user"


#: 沙箱迁移波 3 (spec § 3.1) — 租户没配 WORKSPACE_BYTES_PER_USER 时的
#: 平台默认每用户工作区上限。唯一定义点;闸与前端提示都以此为准。
DEFAULT_WORKSPACE_BYTES_PER_USER: int = 10 * 1024**3


class QuotaPurpose(StrEnum):
    """Workload purpose attached to ``check`` / ``reserve`` for metric labels.

    M0 stores it as a label on metrics + the reservation row but does
    NOT use it to route to separate buckets — that's an M2 change.
    """

    PRODUCTION = "production"
    SUMMARIZATION = "summarization"
    EVAL = "eval"
    JUDGE = "judge"


class ReservationState(StrEnum):
    """``token_reservation.state`` enum (subsystems/16 § 3.1)."""

    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# RPC — check
# ---------------------------------------------------------------------------


class CheckRequest(BaseModel):
    """One ``check`` call: per-request QPS / token bucket decision."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    agent: str | None = None
    user: str | None = None
    model: str | None = None  # M0: recorded only; M1: enters dimensions
    cost: int = Field(default=1, ge=1)
    purpose: QuotaPurpose = QuotaPurpose.PRODUCTION
    # Mini-ADR J-30 (J.6.补强-1) — per-dimension cost override. Maps a
    # :class:`QuotaDimension` to the cost the service should subtract from
    # that dimension's bucket instead of ``cost``. The image upload path
    # uses this to deduct ``file_size`` bytes from ``IMAGE_STORAGE_BYTES``
    # while keeping ``cost=1`` for ``QPS`` / ``IMAGE_UPLOAD_COUNT_30D`` —
    # otherwise a 1 MiB upload would burn a million QPS tokens.
    cost_overrides: dict[QuotaDimension, int] = Field(default_factory=dict)


class CheckResult(BaseModel):
    """``check`` outcome: allow + remaining-by-dim, or deny + retry hint."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    blocked_dimension: QuotaDimension | None = None
    retry_after_s: int | None = Field(default=None, ge=0)
    # Per-dimension remaining (integer token count); empty when no
    # dimensions applied (e.g. tenant has no quota row → unlimited).
    remaining: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# RPC — reserve / commit / release
# ---------------------------------------------------------------------------


class ReserveRequest(BaseModel):
    """Token budget reservation request (subsystems/16 § 5.4)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    agent: str
    thread_id: UUID
    estimated_tokens: int = Field(ge=1)
    model: str | None = None  # M0: stored only
    parent_thread_id: UUID | None = None
    purpose: QuotaPurpose = QuotaPurpose.PRODUCTION


class ReserveResult(BaseModel):
    """``reserve`` outcome: granted + reservation_id, or denied + reason."""

    model_config = ConfigDict(frozen=True)

    granted: bool
    reservation_id: UUID | None = None
    reason: Literal["over_budget", "dimension_blocked", "ok"] = "ok"


class CommitRequest(BaseModel):
    """``commit`` request: finalise a reservation with actual usage.

    ``tenant_id`` is required so the persistence store can verify the
    caller's reservation belongs to them. Subsystems/16 § 3.3 lists it
    in the conceptual schema; in practice the over-the-wire form pulls
    tenant from JWT, but we keep it on the request body so the same
    DTO works for in-process callers too.
    """

    model_config = ConfigDict(frozen=True)

    reservation_id: UUID
    tenant_id: UUID
    actual_tokens: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Records — DB row shapes surfaced over the admin / introspection APIs
# ---------------------------------------------------------------------------


class TenantQuotaRecord(BaseModel):
    """One row of ``tenant_quota`` exposed via the admin endpoints."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    dimension: QuotaDimension
    scope: dict[str, str] = Field(default_factory=dict)
    limit_value: int
    burst: int | None = None
    effective_from: datetime
    effective_until: datetime | None = None
    updated_by: str
    updated_at: datetime


class TenantQuotaPatch(BaseModel):
    """Admin POST payload for ``/v1/tenants/{t}/quotas``."""

    model_config = ConfigDict(frozen=True)

    dimension: QuotaDimension
    scope: dict[str, str] = Field(default_factory=dict)
    limit_value: int = Field(ge=0)
    burst: int | None = Field(default=None, ge=0)
    effective_until: datetime | None = None


class TenantBudgetRecord(BaseModel):
    """One row of ``token_budget_ledger`` (read-only over admin API)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    month: date
    budget_total: int
    used_total: int
    reserved_total: int
    updated_at: datetime


class TokenReservationRecord(BaseModel):
    """One row of ``token_reservation`` (read-only over admin / reaper)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    agent_name: str
    thread_id: UUID
    parent_thread_id: UUID | None = None
    model: str | None = None
    estimated: int
    actual: int | None = None
    state: ReservationState
    reserved_at: datetime
    closed_at: datetime | None = None
