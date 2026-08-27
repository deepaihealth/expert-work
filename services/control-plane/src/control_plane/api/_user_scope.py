"""Per-user scope helpers — Stream J.14.

Resolve the calling principal to a ``tenant_user.id`` and enforce
thread ownership for human-user principals.

A thread stamped with a ``user_id`` is private to that user. Machine
principals (service / service_account) and tenant admins keep
tenant-wide access; threads with no ``user_id`` (created before J.14,
or machine-triggered) keep the legacy tenant-scoped behaviour.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request

from control_plane.auth.rbac import is_admin
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import Principal, ThreadMeta

logger = logging.getLogger("expert_work.control_plane.api.user_scope")


def get_user_repo(request: Request) -> TenantUserStore:
    """FastAPI dependency — the per-user registry store."""
    return request.app.state.tenant_user_repo  # type: ignore[no-any-return]


async def resolve_caller_user_id(request: Request, users: TenantUserStore) -> UUID | None:
    """Return the calling user's ``tenant_user.id``.

    ``None`` for machine principals (service / service_account) — they
    own no per-user instance. For human users this upserts the registry
    row and bumps ``last_active_at``.
    """
    principal: Principal = request.state.principal
    if principal.subject_type != "user":
        return None
    user = await users.resolve(
        tenant_id=principal.tenant_id,
        subject_type=principal.subject_type,
        subject_id=principal.subject_id,
    )
    return user.id


async def resolve_target_user_id(
    request: Request,
    users: TenantUserStore,
    *,
    requested: UUID | None,
) -> UUID | None:
    """The user whose per-user resources this request may act on.

    Conversation-centric IA M2/M3 (H.8-F1) — the shared gate behind
    ``?user_id=`` on per-user endpoints (memory / artifacts list +
    actions): no ``requested`` (or asking for yourself) resolves to the
    caller's own ``tenant_user.id``; a tenant admin may target any
    member (same admin semantics as :func:`caller_owns_thread`); anyone
    else asking for someone
    else is a 403 — explicit, never silent narrowing. ``None`` means a
    machine principal acting with no target (owns nothing itself).
    """
    caller_user_id = await resolve_caller_user_id(request, users)
    if requested is None or requested == caller_user_id:
        return caller_user_id
    if not is_admin(request.state.principal):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "USER_SCOPE_FORBIDDEN",
                "message": "only tenant admins may act on another user's resources",
            },
        )
    return requested


# ``ensure_member_active`` (Stream R R-8 first-run activation) retired
# 2026-08-27 — the invited→active promotion now rides every authenticated
# request via ``control_plane.member_activation.MemberActivationMiddleware``
# (拍板「登录过就算」).


def caller_owns_thread(
    *,
    meta: ThreadMeta,
    caller_user_id: UUID | None,
    principal: Principal,
) -> bool:
    """``True`` if the principal may read / run / mutate ``meta``'s thread."""
    if meta.user_id is None:
        return True  # unowned — legacy tenant-scoped access
    if principal.subject_type != "user":
        return True  # machine principal — tenant-scoped
    if is_admin(principal):
        return True  # tenant admin — tenant-wide access
    return caller_user_id == meta.user_id


def thread_list_filter(*, caller_user_id: UUID | None) -> UUID | None:
    """The ``user_id`` filter for thread *list* endpoints: every caller — plain
    user or admin — sees only their own threads. ``None`` only for a machine
    principal (no owning user); the debug console has no cross-user history."""
    return caller_user_id
