"""Shared resolution + ownership gate for the external (third-party) API plane.

Every ``/v1/agents/{agent_code}/...`` endpoint a third-party API key can reach
goes through here: the app's own ``user_id`` string is resolved to a
``tenant_user`` row, and the addressed resource (session / run) is verified to
belong to that ``(tenant, user, agent)`` triple. A mismatch is 404 — never 403 —
so the response carries no existence information. Mirrors the check
``agents.py:_resolve_session`` already performs for ``session_id``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi.responses import JSONResponse

from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import ThreadMeta
from expert_work.runtime.runs import RunInfo, RunStore

#: Namespace prefix for end-user identities minted from a third-party app's own
#: ``user_id`` string. An employee's ``subject_id`` is a bare Keycloak ``sub``
#: (a UUID), so without this prefix a third party could pass an employee's UUID
#: and reach that employee's console sessions. ``subject_type`` deliberately
#: stays ``"user"``: the user-dimension ops page (``api/agent_users.py``) and the
#: delete-user pipeline (``purge/user_purge.py``) both select on it — a distinct
#: type would hide external users from the former and make them unpurgeable by
#: the latter.
EXTERNAL_SUBJECT_PREFIX = "ext:"


def external_subject_id(user_id: str) -> str:
    """Namespace an app-supplied ``user_id`` for ``tenant_user.subject_id``."""
    return f"{EXTERNAL_SUBJECT_PREFIX}{user_id}"


class ExternalScopeError(Exception):
    """Resolution / ownership failure, converted to an envelope by the endpoint."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def external_error(exc: ExternalScopeError) -> JSONResponse:
    """Render an :class:`ExternalScopeError` as the standard envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": exc.code, "message": exc.message},
        },
    )


async def resolve_external_user_id(
    *, tenant_id: UUID, user_id: str, users: TenantUserStore
) -> UUID:
    """Resolve (mint-on-use) an app-supplied ``user_id`` to ``tenant_user.id``."""
    row = await users.resolve(
        tenant_id=tenant_id,
        subject_type="user",
        subject_id=external_subject_id(user_id),
    )
    return row.id


async def load_owned_session(
    *,
    tenant_id: UUID,
    agent_code: str,
    user_id: str,
    session_id: UUID,
    threads: ThreadMetaStore,
    users: TenantUserStore,
) -> ThreadMeta:
    """Return the session, or raise 404 unless it belongs to ``(user, agent)``."""
    end_user_id = await resolve_external_user_id(tenant_id=tenant_id, user_id=user_id, users=users)
    meta = await threads.get(session_id, tenant_id=tenant_id)
    if meta is None or meta.user_id != end_user_id or meta.agent_name != agent_code:
        raise ExternalScopeError(
            "SESSION_NOT_FOUND", "session not found for this user / agent", 404
        )
    return meta


async def load_owned_run(
    *,
    tenant_id: UUID,
    agent_code: str,
    user_id: str,
    run_id: UUID,
    runs: RunStore,
    threads: ThreadMetaStore,
    users: TenantUserStore,
) -> tuple[RunInfo, ThreadMeta]:
    """Return ``(run, its session)``, or raise 404 unless both belong to ``(user, agent)``.

    A run whose session fails the ownership check reports ``RUN_NOT_FOUND`` — not
    the session's code — so the caller cannot tell "this run exists but is
    someone else's" from "no such run".
    """
    run = await runs.get(run_id=run_id, tenant_id=tenant_id)
    if run is None:
        raise ExternalScopeError("RUN_NOT_FOUND", "run not found", 404)
    try:
        meta = await load_owned_session(
            tenant_id=tenant_id,
            agent_code=agent_code,
            user_id=user_id,
            session_id=run.thread_id,
            threads=threads,
            users=users,
        )
    except ExternalScopeError:
        raise ExternalScopeError("RUN_NOT_FOUND", "run not found", 404) from None
    return run, meta
