"""Thin helpers around :class:`AuditLogger` for the Control Plane.

Constructs an ``AuditLogger`` wired to a SQL or in-memory store + a
redactor (D.2: :class:`TenantAwareRedactor` when a PII-fields resolver
is supplied, :class:`DefaultSecretRedactor` otherwise) + the in-memory
fallback queue. The B.5 handlers call :func:`emit` with the per-request
actor / tenant; redaction + durability fallback are handled by
``AuditLogger`` itself.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from expert_work.persistence.audit_log import AuditLogStore, InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditEntry, AuditResult
from expert_work.runtime.audit.fallback import InMemoryAuditFallbackQueue
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.audit.redactor import (
    AuditRedactor,
    DefaultSecretRedactor,
    PiiFieldsResolver,
    TenantAwareRedactor,
)

if TYPE_CHECKING:
    from control_plane.tenancy import TenantConfigService

logger = logging.getLogger(__name__)


class TenantConfigPiiResolver:
    """Late-bound :class:`PiiFieldsResolver` backed by ``TenantConfigService``.

    The audit logger and the tenant_config service have a circular
    construction dependency:

    * ``AuditLogger`` needs the resolver to wire the
      :class:`TenantAwareRedactor`.
    * ``TenantConfigService`` needs ``AuditLogger`` to emit
      ``tenant_config:read`` / ``write`` audits.

    This adapter resolves the cycle: it's constructed first (returning
    ``[]`` for every tenant when unbound), passed into the audit
    logger, and ``bind()``-ed to the service after both are wired.
    Before binding no audit emissions happen — startup is single-
    threaded — so the empty-list behavior is invisible.
    """

    def __init__(self) -> None:
        self._service: TenantConfigService | None = None

    def bind(self, service: TenantConfigService) -> None:
        """Attach the live ``TenantConfigService``. Idempotent."""
        self._service = service

    async def __call__(self, tenant_id: UUID) -> Sequence[str]:
        if self._service is None:
            return []
        # ``TenantConfigService.get`` raises ``TenantConfigNotConfiguredError``
        # when no row exists — that's a normal "tenant not seeded yet"
        # state, not an error. Return [] so the redactor falls through
        # to global patterns only. Other failures bubble up and are
        # swallowed by :class:`TenantAwareRedactor` itself per the
        # design.
        from control_plane.tenancy import TenantConfigNotConfiguredError

        try:
            record = await self._service.get(tenant_id=tenant_id)
        except TenantConfigNotConfiguredError:
            return []
        return list(record.pii_fields)


def build_default_audit_logger(
    store: AuditLogStore | None = None,
    *,
    pii_fields_resolver: PiiFieldsResolver | None = None,
) -> AuditLogger:
    """Default wiring used by ``create_app`` in tests / single-process dev.

    Production swaps the in-memory store for a SQL one (see
    ``control_plane.main``).

    When ``pii_fields_resolver`` is supplied the redactor is
    :class:`TenantAwareRedactor` and per-tenant ``pii_fields`` are
    masked in addition to the global secret patterns. With no
    resolver the logger uses :class:`DefaultSecretRedactor` alone —
    fine for unit tests that don't exercise per-tenant PII.
    """
    return AuditLogger(
        store=store or InMemoryAuditLogStore(),
        redactor=_build_redactor(pii_fields_resolver),
        fallback=InMemoryAuditFallbackQueue(),
    )


def _build_redactor(resolver: PiiFieldsResolver | None) -> AuditRedactor:
    global_redactor = DefaultSecretRedactor()
    if resolver is None:
        return global_redactor
    return TenantAwareRedactor(
        global_redactor=global_redactor,
        pii_fields_resolver=resolver,
    )


ResourceType = Literal[
    "manifest",
    "session",
    "sandbox",
    "secret",
    "audit",
    "quota",
    "tenant_config",
    "user",
    "role_binding",
    "api_key",
    "service_account",
    "feedback",
    "image_upload",  # Stream J.6.补强-2 — Mini-ADR J-31
    "skill",  # Stream J.7a — Mini-ADR J-23
    # Capability Uplift Sprint #3 (Mini-ADR U-17 supporting files).
    # Separate resource type — supporting-files API has its own surface
    # under /v1/skills/{id}/versions/{v}/supporting-files/{path}.
    "skill_supporting_file",
    # Stream SE (Mini-ADR SE-A2) — replay-verification evidence row.
    # Mirrors the protocol-side ``ResourceType`` Literal per
    # [memory:audit-literal-drift] (both must stay in sync).
    "skill_eval_result",
    # Stream SE (SE-8, Mini-ADR SE-A13b/c) — promote-approval request +
    # persistent kill-switch. Mirrors the protocol-side ``ResourceType``
    # Literal per [memory:audit-literal-drift] (both must stay in sync).
    "skill_promote_request",
    "skill_evolution_kill_switch",
    # Stream J.9-step3 — Mini-ADR J-25 (ARTIFACT_DELETE / ARTIFACT_UPDATE).
    # Was only ever on the protocol side; the three ``emit(resource_type=
    # "artifact")`` call sites (``api/artifacts.py``, ``api/external_artifacts.py``)
    # were silent type errors because CI mypy does not scan control-plane
    # (B-11). Mirrors the protocol-side ``ResourceType`` Literal per
    # [memory:audit-literal-drift] (both must stay in sync).
    "artifact",
    "trigger",  # Stream J.10 — Mini-ADR J-26 / J-42
    "webhook_endpoint",  # HX-9 — STREAM-HX § 13 (outbound webhook hook)
    "eval_dataset",  # Stream J.12 — Mini-ADR J-43
    "curation_candidate",  # Stream J.12 — Mini-ADR J-43
    "system",  # Stream N — Mini-ADR N-5
    "run",  # Stream H.3 PR 1 — Mini-ADR H-6 (RUN_LIST_READ)
    "tenant",  # Stream P — Mini-ADR P-1 (POST /v1/tenants)
    "platform_credential",  # Stream P — Mini-ADR P-11 (/v1/platform/credentials)
    # Capability Uplift Sprint #7 (Mini-ADR U-42) — MemoryConsolidator.
    # Mirrors the protocol-side ``resource_type`` Literal in
    # packages/expert-work-protocol/src/expert_work/protocol/audit.py per
    # [memory:audit-literal-drift] (both must stay in sync).
    "memory_item",
    "tenant_member",  # Stream R — Mini-ADR R-3 (member onboarding)
    "keycloak_user",  # Stream R — Mini-ADR R-3 (Keycloak account provisioning)
    "tenant_mcp_server",  # Stream V — tenant remote MCP server registry
    # Stream W — platform MCP connector catalog. Mirrors the protocol-side
    # ``ResourceType`` Literal in
    # packages/expert-work-protocol/src/expert_work/protocol/audit.py per
    # [memory:audit-literal-drift] (both must stay in sync).
    "mcp_connector_catalog",
    # Stream Y — platform model rate card (Y-3). Mirrors the protocol-side
    # ``ResourceType`` Literal per [memory:audit-literal-drift].
    "model_rate_card",
    # Stream Agent-Templates — platform Agent template catalog (system_admin).
    # Mirrors the protocol-side ``ResourceType`` Literal per
    # [memory:audit-literal-drift].
    "platform_agent_template",
    # Stream TE-2 — per-tool-call audit (TOOL_CALL / TOOL_BLOCKED). Mirrors
    # the protocol-side ``ResourceType`` Literal per [memory:audit-literal-drift].
    "tool",
    # Document upload → workspace (read_document base capability). Mirrors the
    # protocol-side ``ResourceType`` Literal per [memory:audit-literal-drift].
    "user_workspace",
    # Stream RT-4 — agent kill switch (AGENT_DISABLED / AGENT_ENABLED). Mirrors
    # the protocol-side ``ResourceType`` Literal per [memory:audit-literal-drift].
    "agent",
    # Deletion-hygiene PR2 — per-user MCP OAuth connection (was mis-tagged
    # "tenant_mcp_server"). Mirrors the protocol-side ``ResourceType`` Literal
    # per [memory:audit-literal-drift].
    "mcp_oauth_connection",
]


async def emit(
    logger: AuditLogger,
    *,
    tenant_id: UUID,
    actor_id: str,
    action: AuditAction,
    resource_type: ResourceType,
    resource_id: str | None = None,
    result: AuditResult = AuditResult.SUCCESS,
    reason: str | None = None,
    trace_id: str | None = None,
    details: dict[str, object] | None = None,
    on_behalf_of: str | None = None,
) -> None:
    """One-shot helper: build :class:`AuditEntry` + write.

    Defaults ``actor_type="user"`` because the dev-mode middleware does
    not yet distinguish service accounts; Stream C.1 will override per
    JWT principal. ``on_behalf_of`` records the end-user a service-account
    caller acted for (Stream Agent-Templates M1-5b — the external run model).
    """
    entry = AuditEntry(
        tenant_id=tenant_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        reason=reason,
        trace_id=trace_id,
        details=details or {},
        on_behalf_of=on_behalf_of,
    )
    await logger.write(entry)
