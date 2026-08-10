"""Member invite / resend orchestration — Stream R W2 (Mini-ADR R-4).

Shared by the ``/v1/members`` endpoints. Each operation provisions a Keycloak
account and writes a tenant-scope role binding using DB-first + idempotent
compensation: the local ``tenant_member`` row is the source of truth, the
Keycloak side is external and re-tryable. ``resend`` is the single compensation
entry point — it finishes whichever step a prior attempt left incomplete
(account, binding, or email).
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
from expert_work.persistence.auth import DuplicateRoleBindingError, RoleBindingStore
from expert_work.persistence.tenant_member import DuplicateMemberError, TenantMemberStore
from expert_work.protocol import AuditAction, MemberRole, Role, TenantMember
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.api.member_ops")

_ROLE_BY_NAME: dict[MemberRole, Role] = {
    "admin": Role.ADMIN,
    "operator": Role.OPERATOR,
    "viewer": Role.VIEWER,
}


class MemberConflictError(Exception):
    """The email already exists in Keycloak (Mini-ADR R-11) — surfaced as 409."""


class MemberKeycloakUnavailableError(Exception):
    """Keycloak unreachable mid-provision — local row stays re-tryable (502)."""


@dataclass(frozen=True)
class MemberOpResult:
    member_id: UUID
    status: str
    keycloak_user_id: str | None
    initial_password: str | None = None  # password 模式一次性回传;绝不落库/审计/日志


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


async def _provision_keycloak(
    *,
    tenant_id: UUID,
    member: TenantMember,
    email: str,
    role: MemberRole,
    display_name: str | None,
    actor_id: str,
    member_store: TenantMemberStore,
    role_binding_store: RoleBindingStore,
    keycloak: KeycloakAdminClient,
    audit: AuditLogger,
    email_action_lifespan_s: int,
    provisioning_mode: str,
) -> MemberOpResult:
    """Provision the Keycloak account + binding + password/email setup for an
    ``invited`` member.

    Idempotent: skips the account create if ``keycloak_user_id`` is already
    back-filled (resend after a partial failure), and tolerates a duplicate
    binding. Mutates nothing local until the Keycloak account exists.
    """
    kc_user_id = member.keycloak_user_id
    if kc_user_id is None:
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
            raise MemberConflictError(email) from exc
        except KeycloakUnavailableError as exc:
            await _emit(
                audit,
                tenant_id,
                actor_id,
                AuditAction.KEYCLOAK_USER_CREATE_FAILED,
                str(member.id),
                {"email": email, "reason": "unavailable"},
            )
            raise MemberKeycloakUnavailableError(str(exc)) from exc
        kc_user_id = kc_user.id
        await member_store.set_keycloak_user_id(
            member_id=member.id, tenant_id=tenant_id, keycloak_user_id=kc_user_id
        )
        await _emit(
            audit,
            tenant_id,
            actor_id,
            AuditAction.KEYCLOAK_USER_CREATE,
            str(member.id),
            {"email": email},
        )

    # Role binding — tolerate a duplicate from a prior partial attempt.
    try:
        await role_binding_store.create(
            subject_type="user",
            subject_id=UUID(kc_user_id),
            tenant_id=tenant_id,
            role=_ROLE_BY_NAME[role],
            granted_by=actor_id,
            platform_scope=False,
        )
    except DuplicateRoleBindingError:
        # Idempotent: a prior partial attempt already wrote this binding.
        logger.debug("member.binding_already_exists member_id=%s", member.id)

    initial_password: str | None = None
    if provisioning_mode == "password":
        # password 模式:临时密码替代 set-password 邮件;temporary=True 首登强制改密。
        # 失败语义与邮件分支一致——不回滚,resend 重驱(届时重新生成)。
        candidate = generate_initial_password()
        try:
            await keycloak.reset_password(user_id=kc_user_id, password=candidate, temporary=True)
            initial_password = candidate
        except KeycloakUnavailableError:
            logger.warning(
                "member.initial_password_failed member_id=%s (resend can retry)", member.id
            )
    else:
        # Set-password email — failure does not roll back; resend can retry.
        try:
            await keycloak.send_setup_email(user_id=kc_user_id, lifespan_s=email_action_lifespan_s)
        except KeycloakUnavailableError:
            logger.warning("member.setup_email_failed member_id=%s (resend can retry)", member.id)

    return MemberOpResult(
        member_id=member.id,
        status="invited",
        keycloak_user_id=kc_user_id,
        initial_password=initial_password,
    )


async def invite_member(
    *,
    tenant_id: UUID,
    email: str,
    role: MemberRole,
    display_name: str | None,
    actor_id: str,
    member_store: TenantMemberStore,
    role_binding_store: RoleBindingStore,
    keycloak: KeycloakAdminClient,
    audit: AuditLogger,
    email_action_lifespan_s: int,
    provisioning_mode: str,
) -> MemberOpResult:
    """Invite a new member: write the roster row (invited) then provision Keycloak."""
    try:
        member = await member_store.create(
            tenant_id=tenant_id,
            email=email,
            role=role,
            invited_by=actor_id,
            display_name=display_name,
        )
    except DuplicateMemberError as exc:
        # An active invite already exists for this email.
        raise MemberConflictError(email) from exc

    result = await _provision_keycloak(
        tenant_id=tenant_id,
        member=member,
        email=email,
        role=role,
        display_name=display_name,
        actor_id=actor_id,
        member_store=member_store,
        role_binding_store=role_binding_store,
        keycloak=keycloak,
        audit=audit,
        email_action_lifespan_s=email_action_lifespan_s,
        provisioning_mode=provisioning_mode,
    )
    await _emit(
        audit,
        tenant_id,
        actor_id,
        AuditAction.MEMBER_INVITE,
        str(member.id),
        {"email": email, "role": role, "keycloak_user_id": result.keycloak_user_id},
    )
    return result


async def resend_member(
    *,
    member: TenantMember,
    actor_id: str,
    member_store: TenantMemberStore,
    role_binding_store: RoleBindingStore,
    keycloak: KeycloakAdminClient,
    audit: AuditLogger,
    email_action_lifespan_s: int,
    provisioning_mode: str,
) -> MemberOpResult:
    """Re-drive an ``invited`` member's Keycloak provisioning (idempotent compensation)."""
    result = await _provision_keycloak(
        tenant_id=member.tenant_id,
        member=member,
        email=member.email,
        role=member.role,
        display_name=member.display_name,
        actor_id=actor_id,
        member_store=member_store,
        role_binding_store=role_binding_store,
        keycloak=keycloak,
        audit=audit,
        email_action_lifespan_s=email_action_lifespan_s,
        provisioning_mode=provisioning_mode,
    )
    await _emit(
        audit,
        member.tenant_id,
        actor_id,
        AuditAction.MEMBER_RESEND,
        str(member.id),
        {"email": member.email},
    )
    return result
