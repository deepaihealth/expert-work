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


def normalize_external_user_id(user_id: str) -> str:
    """Strip an app-supplied ``user_id`` before it becomes a ``subject_id``.

    Without this, ``"cust-77"`` and ``"cust-77 "`` (trailing space) mint two
    distinct ``tenant_user`` rows. ``external_subject_id`` — the single
    choke point both the mint (write, ``agents.py:_resolve_session``) and
    lookup (read, ``external_sessions.py``) paths pass through — calls this
    first, so there is exactly one normalization rule instead of two
    definitions that can drift apart (External-API-v1 P1 review, Important:
    the write-path implementer refused a read-only-side fix for this exact
    reason — normalizing only reads would make a space-suffixed ``user_id``
    findable under one identity but written under another, worse than doing
    nothing).
    """
    return user_id.strip()


def external_subject_id(user_id: str) -> str:
    """Namespace an app-supplied ``user_id`` for ``tenant_user.subject_id``.

    Raises ``ValueError`` when ``user_id`` normalizes to empty (e.g. it is
    all whitespace) — callers must turn that into a 4xx with a
    machine-readable code rather than minting a namespaced identity for
    blank input.
    """
    normalized = normalize_external_user_id(user_id)
    if not normalized:
        raise ValueError("user_id must not be blank")
    return f"{EXTERNAL_SUBJECT_PREFIX}{normalized}"


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


def _external_subject_id_or_422(user_id: str) -> str:
    """``external_subject_id``, translating its ``ValueError`` into the
    shared :class:`ExternalScopeError` envelope every external endpoint
    already knows how to render, so a blank ``user_id`` surfaces as a 422
    with a machine-readable code instead of a 500."""
    try:
        return external_subject_id(user_id)
    except ValueError as exc:
        raise ExternalScopeError("INVALID_USER_ID", str(exc), 422) from exc


async def resolve_external_user_id(
    *, tenant_id: UUID, user_id: str, users: TenantUserStore
) -> UUID:
    """Resolve (mint-on-use) an app-supplied ``user_id`` to ``tenant_user.id``."""
    row = await users.resolve(
        tenant_id=tenant_id,
        subject_type="user",
        subject_id=_external_subject_id_or_422(user_id),
    )
    return row.id


async def lookup_external_user_id(
    *, tenant_id: UUID, user_id: str, users: TenantUserStore
) -> UUID | None:
    """Look up (never mint) an app-supplied ``user_id``'s ``tenant_user.id``.

    Unlike :func:`resolve_external_user_id`, an unrecognized ``user_id``
    returns ``None`` instead of creating a row. ``GET
    /v1/agents/{code}/sessions?user_id=<anything>`` used to call the mint-on-
    use resolver, so any string a third party enumerated wrote a
    ``tenant_user`` row — cheap to spam and the ghost rows surface on the
    user-dimension ops page (External-API-v1 P1 review, T3, Important). A
    still-blank ``user_id`` (see :func:`normalize_external_user_id`) is a
    client error, not "unknown user", so it still raises
    :class:`ExternalScopeError` rather than returning ``None``.

    Uses ``TenantUserStore.get_by_subject`` — an indexed point lookup on
    ``tenant_user_identity_uniq``, O(1) regardless of tenant size. This used
    to scan ``list_by_tenant(limit=MAX_LIST_LIMIT)`` instead: a tenant with
    more than 500 active users (the third-party mint-one-row-per-end-user
    model makes this the norm, not an edge case) would have a session lookup
    for any user past the first 500 most-recently-active silently return
    "no sessions" — a correctness ceiling, not a documented limit.

    ``get_by_subject`` itself does not filter ``deleted_at`` (it mirrors
    ``get``'s semantics — see ``base.py``), so a soft-deactivated (purged)
    identity is filtered out here instead, to match ``list_by_tenant``'s
    prior behavior: a purged user's sessions must stay unreachable through
    this read plane until they act again and ``resolve`` reactivates them.
    """
    subject_id = _external_subject_id_or_422(user_id)
    row = await users.get_by_subject(
        tenant_id=tenant_id, subject_type="user", subject_id=subject_id
    )
    if row is None or row.deleted_at is not None:
        return None
    return row.id


async def load_owned_session(
    *,
    tenant_id: UUID,
    agent_code: str,
    user_id: str,
    session_id: UUID,
    threads: ThreadMetaStore,
    users: TenantUserStore,
    mint: bool = True,
) -> ThreadMeta:
    """Return the session, or raise 404 unless it belongs to ``(user, agent)``.

    ``mint`` (default ``True``) picks which of the two end-user resolution
    semantics applies, and both are load-bearing — do not collapse this to
    one behavior:

    - ``mint=True`` — the default, used by every endpoint that can create or
      mutate state (session bind, run submit, upload, approval decision,
      run cancel). A third party never pre-registers its end-users, so the
      *first* call under a fresh ``user_id`` must mint the ``tenant_user``
      row (mint-on-use is intentional product behavior here).
    - ``mint=False`` — used only by the read-only message-history endpoint.
      A GET must never write; an unrecognized ``user_id`` here means "no
      such session", not "create a user and then report 404 anyway".
    """
    if mint:
        end_user_id = await resolve_external_user_id(
            tenant_id=tenant_id, user_id=user_id, users=users
        )
    else:
        looked_up = await lookup_external_user_id(tenant_id=tenant_id, user_id=user_id, users=users)
        if looked_up is None:
            raise ExternalScopeError(
                "SESSION_NOT_FOUND", "session not found for this user / agent", 404
            )
        end_user_id = looked_up
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
