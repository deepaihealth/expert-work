"""HX-9 租户级出站 webhook hook — DTOs.

STREAM-HX-DESIGN § 13 (Mini-ADR HX-J0~J5). The platform signs and POSTs
agent-lifecycle events to a tenant-registered URL — the outbound dual of
the J.10 inbound triggers.

These DTOs are the wire shape between the webhook CRUD API
(:class:`WebhookEndpointSpec` is the create payload), the stores (rows are
:class:`WebhookEndpointRecord` / :class:`WebhookDeliveryRecord`), and the
delivery worker.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "WebhookDeliveryRecord",
    "WebhookDeliveryStatus",
    "WebhookEndpointRecord",
    "WebhookEndpointSource",
    "WebhookEndpointSpec",
    "WebhookEventType",
]

#: The agent-lifecycle events a tenant can subscribe to (HX-9 起步三类 +
#: run 终态分 success/failure). All ride the single ``run_event`` spine
#: (STREAM-HX-DESIGN § 13.2.2 方案 a). New types are additive.
WebhookEventType = Literal[
    "run.completed",
    "run.failed",
    "approval.requested",
    "artifact.saved",
    # SE-16 PR-8 — a pending skill visibility-promote request awaits review
    # (agent_private → tenant). Lets business systems route the approval to
    # their own channels; the delivery channel backlog is tracked separately.
    "skill_promote.requested",
    # RT-5 PR-2 (RT-ADR-25) — an agent's recent quality mean dropped below its
    # baseline (production quality monitor). Not a run event: the delivery
    # carries ``run_id=None`` and a synthesised ``event_id``.
    "quality.drift",
]

#: Where an endpoint row came from — the CRUD API (default) or, in a
#: future manifest-reconcile path, an agent manifest. The manifest
#: ``hooks`` field is deprecated in favour of API registration
#: (Mini-ADR HX-J0); ``manifest`` is reserved for that later bridge.
WebhookEndpointSource = Literal["manifest", "api"]

#: How the delivery body is shaped for the endpoint. ``generic`` is the
#: signed expert_work envelope (the historical shape); the IM formats render the
#: event as a plain-text bot message for the platform's incoming-webhook
#: robots (Feishu/Lark, DingTalk, WeCom) so approval / promote events reach
#: humans without a receiver service. The HMAC signature header is still
#: sent (IM platforms ignore unknown headers).
WebhookPayloadFormat = Literal["generic", "feishu", "dingtalk", "wecom"]


class WebhookEndpointSpec(BaseModel):
    """The webhook-endpoint CRUD create payload.

    ``agent_name = None`` subscribes to events from *every* agent in the
    tenant; a value scopes the endpoint to one agent. ``event_types`` is
    the subscription filter — at least one type is required (an endpoint
    subscribing to nothing is a no-op, rejected at the API layer).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="Endpoint name — unique per tenant.")
    url: str = Field(min_length=1, max_length=2048, description="Tenant delivery URL (https).")
    event_types: tuple[WebhookEventType, ...] = Field(
        min_length=1, description="Subscribed event types — at least one."
    )
    agent_name: str | None = Field(
        default=None, description="Scope to one agent, or None for all agents in the tenant."
    )
    enabled: bool = True
    payload_format: WebhookPayloadFormat = Field(
        default="generic",
        description="Delivery body shape — expert_work envelope or an IM bot message.",
    )


class WebhookEndpointRecord(BaseModel):
    """One row of ``webhook_endpoint`` — a registered delivery target.

    ``secret_ref`` points into the :class:`SecretStore` where the HMAC
    signing secret lives (encrypted at rest) — the delivery worker reads
    it back to sign outbound requests (HMAC-SHA256 over the body). It is
    **not** a one-way hash: outbound signing needs the plaintext, unlike
    the J.10 inbound-trigger path which only verifies a hash. The secret
    plaintext is shown once at creation (Mini-ADR HX-J5).
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    user_id: UUID | None = None
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    event_types: tuple[WebhookEventType, ...] = Field(min_length=1)
    agent_name: str | None = None
    secret_ref: str | None = None
    enabled: bool = True
    source: WebhookEndpointSource = "api"
    payload_format: WebhookPayloadFormat = "generic"
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryStatus(StrEnum):
    """Lifecycle status of a :class:`WebhookDeliveryRecord` — one delivery.

    ``PENDING`` — enqueued, not yet attempted. ``DELIVERING`` — claimed by a
    worker replica via the CAS ``claim_ready`` (W1-PR1, multi-replica
    readiness) and in flight; a replica that crashes mid-delivery leaves the
    row here until the next sweep's stale-claim window reclaims it.
    ``DELIVERED`` — a 2xx was received. ``FAILED`` — a non-retryable outcome
    (4xx, Mini-ADR HX-J2). ``RETRYING`` — a 5xx / timeout is queued for a
    backoff re-try (``next_retry_at`` set). ``DEAD_LETTER`` — terminal after
    the retry budget is spent.
    """

    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD_LETTER = "dead_letter"


class WebhookDeliveryRecord(BaseModel):
    """One row of ``webhook_delivery`` — a single event→endpoint delivery.

    ``event_id`` is the source event's stable identity (``{run_id}:{seq}``
    of the originating ``run_event`` frame); ``(endpoint_id, event_id)`` is
    unique so re-scanning the event spine enqueues idempotently. The DLQ
    retry state (``attempt`` / ``next_retry_at`` / ``status``) lives here.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    endpoint_id: UUID
    event_id: str = Field(min_length=1, max_length=256)
    event_type: WebhookEventType
    run_id: UUID | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    status: WebhookDeliveryStatus = WebhookDeliveryStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    response_status: int | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
