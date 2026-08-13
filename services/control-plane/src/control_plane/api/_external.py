"""Shared resolution + ownership gate for the external (third-party) API plane.

Every ``/v1/agents/{agent_code}/...`` endpoint a third-party API key can reach
goes through here: the app's own ``user_id`` string is resolved to a
``tenant_user`` row, and the addressed resource (session / run) is verified to
belong to that ``(tenant, user, agent)`` triple. A mismatch is 404 — never 403 —
so the response carries no existence information. Mirrors the check
``agents.py:_resolve_session`` already performs for ``session_id``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import ThreadMeta
from expert_work.runtime.runs import RunInfo, RunStore

#: The one byte a Postgres ``text`` / ``jsonb`` column cannot store. Postgres
#: represents ``text`` internally as a NUL-terminated C string, so asyncpg
#: raises ``CharacterNotInRepertoireError: invalid byte sequence for encoding
#: "UTF8": 0x00`` on ANY bound parameter that contains it — a SELECT's
#: WHERE-clause comparison crashes exactly like an INSERT/UPDATE does (this is
#: why a bare ``user_id=a%00b`` on a *read* endpoint 500s too, not just the
#: ``PATCH .../sessions/{id}`` write). ``app.py`` registers no fallback
#: ``@app.exception_handler(Exception)`` (by design — see its own comment), so
#: an uncaught instance of that error escapes as Starlette's bare-text
#: "Internal Server Error", breaking the external plane's ``{success, data,
#: error}`` envelope contract.
#:
#: Only NUL is rejected here — not the rest of the C0 control-character range
#: (``\x01``-``\x1F`` other than NUL, ``\x7F``). Those bytes are valid UTF-8
#: and Postgres stores them in ``text`` / ``jsonb`` without complaint, so
#: blocking them would fix no crash — and several external fields are
#: *documented* to carry free-form text verbatim (an email body, a
#: support-ticket description — ``input`` / ``inputs`` / ``untrusted_content``),
#: where ``\n`` / ``\t`` / ``\r`` and other embedded control bytes are
#: completely legitimate content, not attacks. NUL is the one byte with no
#: legitimate use in any of these fields and the one byte that actually
#: crashes the write.
_NUL = "\x00"


def reject_nul(value: str, *, field: str = "value") -> str:
    """Raise ``ValueError`` if ``value`` embeds a NUL byte (``\\x00``); else
    return it unchanged.

    Pydantic-compatible: wrap this in a ``@field_validator`` on any
    external-plane request-body field that ultimately lands in a Postgres
    ``text`` / ``jsonb`` column. Raised as ``ValueError`` (not
    :class:`ExternalScopeError`) so it flows through FastAPI's normal
    body-parsing path — the resulting ``pydantic.ValidationError`` becomes a
    ``RequestValidationError``, which ``app.py``'s existing
    ``@app.exception_handler(RequestValidationError)`` already renders as the
    external envelope's ``422 INVALID_REQUEST`` for every ``/v1/agents/...``
    route. For a query / header / form parameter that is not a ``BaseModel``
    field (so there is no validator to attach this to), call this directly
    and translate the raised ``ValueError`` yourself — ``external_subject_id``
    below and ``agents.py``'s ``Idempotency-Key`` header check both do this.
    """
    if _NUL in value:
        raise ValueError(f"{field} must not contain a NUL byte (\\x00)")
    return value


def reject_nul_deep(value: Any, *, field: str = "value") -> Any:
    """Recursive form of :func:`reject_nul` for a JSON-shaped field (a
    ``dict[str, Any]`` such as ``inputs`` / ``modified_args``, or a
    ``list[str]`` such as ``untrusted_content``) whose leaves are not
    necessarily ``str`` themselves.

    Checks dict KEYS as well as values — a JSON object key is user-controlled
    input here exactly like a value is, and JSONB rejects an embedded NUL in
    either position. Returns ``value`` unchanged (raises on the first NUL
    found); intended to be called for its side effect from a
    ``@field_validator``, mirroring :func:`reject_nul`'s calling convention.
    """
    if isinstance(value, str):
        reject_nul(value, field=field)
    elif isinstance(value, dict):
        for key, val in value.items():
            reject_nul(key, field=field)
            reject_nul_deep(val, field=field)
    elif isinstance(value, list):
        for item in value:
            reject_nul_deep(item, field=field)
    return value


def reject_nul_path_params(request: Request) -> None:
    """Router-level guard: reject a NUL byte (``\\x00``) in ANY path parameter
    on the route it is attached to.

    External-API-v1 P2-b review (Critical) — the P2-b pass above hardened
    every external-plane body / query / header field against an embedded
    NUL, but missed the one input class that isn't a pydantic field or a
    ``Query(...)``/header at all: a **path** parameter. ``POST
    /v1/agents/support%00bot/sessions`` 404s on its face, but the decoded
    NUL survives into ``agent_code`` and flows straight into
    ``AgentDisableStore.get`` / ``AgentSpecStore.list_by_tenant`` (both a
    ``text``-column ``WHERE`` comparison) — the exact ``asyncpg
    CharacterNotInRepertoireError`` → bare-text 500 this whole module exists
    to prevent, on both the read and write side of ``_resolve_session``.
    ``POST /v1/agents/{name}/disable|enable`` has the identical hole via the
    same ``list_by_tenant`` call, on a route that lives outside every
    ``external_*.py`` router (see this module's own docstring on those two).

    Intended to be passed exactly ONCE, in a router's own
    ``APIRouter(..., dependencies=[Depends(reject_nul_path_params)])``
    constructor — never per-route — so a route added to that router later is
    covered by construction, the same way the body/query fields above are
    covered by a shared validator function rather than by remembering to
    call ``reject_nul`` at each new call site. ``tests/test_external_route_...``-
    style self-audits (``test_external_path_param_nul_guard.py``) assert
    every third-party-reachable route actually carries this dependency, so a
    future route that is mounted on the wrong router (or a router that drops
    this from its constructor) fails CI instead of silently reopening this
    hole.

    Reads ``request.path_params`` directly rather than the endpoint's own
    typed parameters: Starlette populates that dict from the raw, already
    percent-decoded URL during routing — BEFORE FastAPI/pydantic coerces a
    ``{run_id}`` segment to ``UUID`` or leaves a ``{agent_code}`` segment as
    ``str`` — so every path segment is a plain ``str`` here regardless of
    what type the endpoint eventually asks for. That is what lets one
    dependency check every path param on every route without knowing each
    route's parameter names or types; a UUID-typed param with an embedded
    NUL is caught here as readily as ``agent_code``, which is redundant with
    (but no less correct than) FastAPI's own UUID parsing rejecting it.

    Raises :class:`fastapi.exceptions.RequestValidationError` — NOT a bare
    ``ValueError`` (which a plain ``Depends(...)`` callable raising it would
    NOT have translated into the external envelope; only pydantic's own
    validation errors are caught by ``app.py``'s
    ``@app.exception_handler(RequestValidationError)``) and not
    :class:`ExternalScopeError` (that class is rendered by each endpoint's
    own ``try/except`` — a router-level dependency runs before any endpoint
    code and has no such block to be caught by). Constructing this exact
    exception type is what lets a router-level dependency reuse the SAME
    rendering path every body-field ``field_validator`` above already goes
    through, verified with a real request rather than assumed.
    """
    for name, value in request.path_params.items():
        if not isinstance(value, str):
            continue
        try:
            reject_nul(value, field=name)
        except ValueError as exc:
            raise RequestValidationError(
                [{"loc": ("path", name), "msg": str(exc), "type": "value_error"}]
            ) from exc


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
    blank input. Also raises (via :func:`reject_nul`) when ``user_id``
    embeds a NUL byte — this is the single choke point every external
    endpoint's ``user_id`` (query, form, or body field) passes through
    (``resolve_external_user_id`` / ``lookup_external_user_id`` both call
    this), so the guard lives here once rather than being repeated at every
    call site.
    """
    normalized = normalize_external_user_id(user_id)
    if not normalized:
        raise ValueError("user_id must not be blank")
    return reject_nul(f"{EXTERNAL_SUBJECT_PREFIX}{normalized}", field="user_id")


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

    The dividing line is **not** read-vs-write, it is *does this call create
    the session it addresses*:

    - ``mint=True`` — the semantics for a call that brings a session into
      existence: session bind, run submit and upload, **but only on the branch
      where they omit ``session_id``**. A third party never pre-registers its
      end-users, so the first call under a fresh ``user_id`` must mint the
      ``tenant_user`` row; mint-on-use is intentional product behavior there.
      Those branches mint through ``agents.py:_resolve_session``, which has to
      create the thread in the same breath, rather than through this function
      — so this is the documented counterpart of the rule below, not a mode
      the external plane currently exercises here.
    - ``mint=False`` — for every caller handed an **already-existing** session
      or run id: message history, :func:`load_owned_run` (cancel / events /
      approval-decide), and the upload / session-bind / run-submit paths that
      *supply* ``session_id``. Such a resource's owner necessarily already has
      a ``tenant_user`` row, so there is nothing to mint; the only row minting
      could add is one for a ``user_id`` that is not the owner — exactly the
      case that must 404. Doing it anyway is how enumerating ``user_id``s
      against a known id left one ghost row per attempt, and how a call that
      answered 404 still resurrected a purged identity (P1 final review C1 —
      read plane + upload; wrap-up N1 — session bind + run submit, which take
      this branch inside ``_resolve_session`` rather than by calling here).
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

    Resolution is ``mint=False`` unconditionally, and that is structural, not a
    per-caller preference: **a run that exists already has an owner with a
    ``tenant_user`` row**, so there is nothing for this function to mint. Minting
    here only ever creates a row for a ``user_id`` that is by definition *not*
    the run's owner — i.e. exactly the enumeration case this must 404. With the
    default ``mint=True`` it did three wrong things at once (P1 final review,
    C1): a third party could spray arbitrary ``user_id``s at ``GET
    .../runs/{id}/events``, get 404 every time, and still leave one ghost row
    per attempt on the user-dimension ops page; a soft-deleted (purged) identity
    was *resurrected* by that GET (``resolve`` clears ``deleted_at`` — the
    "returning user comes back clean" design), which also made it permanently
    uncollectable by the ``deleted_at``-driven Phase-3b hard delete; and,
    because resurrection happened before the ownership check, the purged user's
    own run events stayed readable (200) while their messages and session list
    correctly reported 404 / empty. The mint belongs to the branch that
    genuinely creates a session (``_resolve_session`` with no ``session_id``),
    never here.
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
            mint=False,
        )
    except ExternalScopeError:
        raise ExternalScopeError("RUN_NOT_FOUND", "run not found", 404) from None
    return run, meta
