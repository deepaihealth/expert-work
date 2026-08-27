"""Member activation on first login — 2026-08-27 拍板.

Stream R (R-8) originally flipped a roster row ``invited → active`` on the
member's **first run** (the hook lived in the console run-create endpoint).
Owner decision 2026-08-27: "登录过就算" — a member who has authenticated at
all has shown up, whether or not they ever sent a run. So the hook moves to
the request path: any request carrying that user's verified JWT promotes
their ``invited`` roster row.

Cost control: a per-pod memo of subject ids that have already reached a
definitive outcome (activated / already active / no roster row), so the
member lookup runs once per user per pod lifetime, not per request. The
transition itself is CAS-guarded in the store, so two pods racing the same
first login is a harmless double-attempt. A store error is logged and never
blocks the request — activation is bookkeeping, not authorization.

Placement (app.py middleware order): INSIDE ``RLSContextMiddleware`` so the
``tenant_user`` upsert runs with the request's tenant context; the member
lookup itself uses ``bypass_rls_session()`` because the roster row is keyed
by Keycloak id, not the request's tenant scope (same rationale as the old
run-path hook).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from control_plane.tenant_scope import bypass_rls_session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from expert_work.persistence.tenant_member import TenantMemberStore
    from expert_work.persistence.tenant_user import TenantUserStore
    from expert_work.protocol import Principal

logger = logging.getLogger(__name__)


class MemberActivationMiddleware(BaseHTTPMiddleware):
    """Promote an ``invited`` roster member to ``active`` on first login."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        member_repo: TenantMemberStore | None,
        users: TenantUserStore | None,
    ) -> None:
        super().__init__(app)
        self._member_repo = member_repo
        self._users = users
        # Subject ids with a definitive outcome — skip the lookup forever
        # (per pod). Bounded by the real user population.
        self._seen: set[str] = set()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        principal: Principal | None = getattr(request.state, "principal", None)
        if (
            self._member_repo is not None
            and self._users is not None
            and principal is not None
            and principal.subject_type == "user"
            and principal.subject_id not in self._seen
        ):
            try:
                await self._activate(principal)
            except Exception:
                # Never block the request on roster bookkeeping; no memo add,
                # so the next request retries.
                logger.warning("member.activation_failed", exc_info=True)
        return await call_next(request)

    async def _activate(self, principal: Principal) -> None:
        if self._member_repo is None or self._users is None:  # dispatch already gates
            return
        async with bypass_rls_session():
            member = await self._member_repo.get_by_keycloak_user_id(
                keycloak_user_id=principal.subject_id
            )
        if member is None or member.status != "invited":
            # No roster row (e.g. bootstrap admin) or already past invited —
            # definitive either way.
            self._seen.add(principal.subject_id)
            return
        # Back-fill ``subject_id`` with the resolved ``tenant_user.id``
        # (Mini-ADR R-6) — same semantics as the old run-path hook.
        user = await self._users.resolve(
            tenant_id=principal.tenant_id,
            subject_type=principal.subject_type,
            subject_id=principal.subject_id,
        )
        async with bypass_rls_session():
            moved = await self._member_repo.transition(
                member_id=member.id,
                tenant_id=member.tenant_id,
                to="active",
                now=datetime.now(UTC),
                subject_id=user.id,
            )
        self._seen.add(principal.subject_id)
        if moved:
            logger.info(
                "member.activated member_id=%s tenant_id=%s trigger=first_login",
                member.id,
                member.tenant_id,
            )
