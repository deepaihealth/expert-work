"""Stream R W1 — provision a tenant's first admin alongside tenant creation.

``POST /v1/tenants`` may carry a ``first_admin_email``. When it does, the
handler must, in one logical step: create the tenant, provision a Keycloak
account for that admin, write a tenant-scope ``ADMIN`` role binding, and send
the set-password email.

There is no distributed transaction across expert_work's DB and Keycloak, so this
uses **DB-first + idempotent compensation** (Mini-ADR R-4):

1. the tenant row + the ``tenant_member`` roster row (``invited``) are written
   first, inside the caller's ``bypass_rls_session()`` — the local state is
   atomic and consistent even if every later step fails;
2. the Keycloak account is provisioned (external, retryable);
3. the ``keycloak_user_id`` is back-filled, the cross-tenant role binding is
   written (Mini-ADR R-5), and the set-password email is sent.

Any failure after step 1 leaves the system in a locally-consistent, re-tryable
state (``invited`` member, ``keycloak_user_id`` possibly NULL); the W2
``resend`` endpoint is the single compensation entry point. We never create the
Keycloak account before the local rows exist — an external side effect cannot
be rolled back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from control_plane.audit import emit
from control_plane.keycloak import (
    KeycloakAdminClient,
    KeycloakUnavailableError,
    KeycloakUserExistsError,
)
from control_plane.member_password import generate_initial_password
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.auth import RoleBindingStore
from expert_work.persistence.tenant_member import DuplicateMemberError, TenantMemberStore
from expert_work.protocol import AuditAction, Role, TenantMember
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.api.first_admin")


class FirstAdminConflictError(Exception):
    """The admin email already exists in Keycloak (Mini-ADR R-11).

    The tenant has been created; the caller surfaces this as 409 so the admin
    can retry with a different email. The roster row stays ``invited`` with a
    NULL ``keycloak_user_id``.
    """

    def __init__(self, email: str) -> None:
        super().__init__(f"first admin email already exists in keycloak: {email!r}")
        self.email = email


class FirstAdminKeycloakUnavailableError(Exception):
    """Keycloak was unreachable while provisioning the admin account.

    The tenant + ``invited`` roster row exist; ``resend`` finishes the Keycloak
    side. Surfaced as 502.
    """


@dataclass(frozen=True)
class FirstAdminResult:
    """What the handler echoes back about the provisioned admin."""

    member_id: UUID
    email: str
    status: str
    keycloak_user_id: str | None
    initial_password: str | None = None  # password 模式一次性回传;绝不落库/审计/日志


async def provision_first_admin(
    *,
    tenant_id: UUID,
    email: str,
    display_name: str | None,
    actor_id: str,
    member_store: TenantMemberStore,
    role_binding_store: RoleBindingStore,
    keycloak: KeycloakAdminClient,
    audit: AuditLogger,
    email_action_lifespan_s: int,
    provisioning_mode: str,
) -> FirstAdminResult:
    """Provision the first tenant admin. Call inside ``bypass_rls_session()``.

    Steps 1 (member row) runs in the caller's bypass session alongside the
    tenant create; steps 2-4 (Keycloak + binding + email) follow. Raises
    :class:`FirstAdminConflictError` / :class:`FirstAdminKeycloakUnavailableError`
    on the recoverable failure points described in the module docstring.
    """
    # Step 1 — local roster row (invited). DuplicateMemberError is impossible
    # for a brand-new tenant, but map it defensively.
    try:
        member: TenantMember = await member_store.create(
            tenant_id=tenant_id,
            email=email,
            role="admin",
            invited_by=actor_id,
            display_name=display_name,
        )
    except DuplicateMemberError as exc:  # pragma: no cover - new tenant
        raise FirstAdminConflictError(email) from exc

    # Step 2 — provision the Keycloak account (external, retryable).
    try:
        kc_user = await keycloak.create_user(
            email=email,
            tenant_id=tenant_id,
            display_name=display_name,
            email_verified=(provisioning_mode == "password"),
        )
    except KeycloakUserExistsError as exc:
        await _emit(
            audit,
            tenant_id,
            actor_id,
            AuditAction.KEYCLOAK_USER_CREATE_FAILED,
            str(member.id),
            {"email": email, "reason": "exists"},
        )
        raise FirstAdminConflictError(email) from exc
    except KeycloakUnavailableError as exc:
        await _emit(
            audit,
            tenant_id,
            actor_id,
            AuditAction.KEYCLOAK_USER_CREATE_FAILED,
            str(member.id),
            {"email": email, "reason": "unavailable"},
        )
        raise FirstAdminKeycloakUnavailableError(str(exc)) from exc

    # Step 3 — back-fill the Keycloak id + write the cross-tenant ADMIN binding
    # (Mini-ADR R-5: explicit target tenant_id, already inside bypass_rls).
    await member_store.set_keycloak_user_id(
        member_id=member.id, tenant_id=tenant_id, keycloak_user_id=kc_user.id
    )
    await role_binding_store.create(
        subject_type="user",
        subject_id=UUID(kc_user.id),
        tenant_id=tenant_id,
        role=Role.ADMIN,
        granted_by=actor_id,
        platform_scope=False,
    )

    # Step 4 — password/email handoff. Failure here does NOT roll back (account
    # + binding already exist); resend can retry. Log and continue.
    initial_password: str | None = None
    if provisioning_mode == "password":
        # password 模式:临时密码替代 set-password 邮件;temporary=True 首登强制改密。
        # 失败语义与邮件分支一致——不回滚,resend 是唯一补偿入口(见模块 docstring)。
        candidate = generate_initial_password()
        try:
            await keycloak.reset_password(user_id=kc_user.id, password=candidate, temporary=True)
            initial_password = candidate
        except KeycloakUnavailableError:
            logger.warning(
                "first_admin.initial_password_failed member_id=%s (resend can retry)", member.id
            )
    else:
        # Set-password email — failure does not roll back; resend can retry.
        try:
            await keycloak.send_setup_email(user_id=kc_user.id, lifespan_s=email_action_lifespan_s)
        except KeycloakUnavailableError:
            logger.warning(
                "first_admin.setup_email_failed member_id=%s (resend can retry)", member.id
            )

    await _emit(
        audit,
        tenant_id,
        actor_id,
        AuditAction.KEYCLOAK_USER_CREATE,
        str(member.id),
        {"email": email},
    )
    await _emit(
        audit,
        tenant_id,
        actor_id,
        AuditAction.MEMBER_INVITE,
        str(member.id),
        {"email": email, "role": "admin", "keycloak_user_id": kc_user.id},
    )
    return FirstAdminResult(
        member_id=member.id,
        email=email,
        status="invited",
        keycloak_user_id=kc_user.id,
        initial_password=initial_password,
    )


async def _emit(
    audit: AuditLogger,
    tenant_id: UUID,
    actor_id: str,
    action: AuditAction,
    resource_id: str,
    details: dict[str, object],
) -> None:
    resource_type = (
        "keycloak_user" if action.value.startswith("keycloak_user:") else "tenant_member"
    )
    await emit(
        audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,  # type: ignore[arg-type]
        resource_id=resource_id,
        trace_id=current_trace_id_hex(),
        details=details,
    )
