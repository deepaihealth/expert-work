"""Request-scoped tenant + trace context for observability propagation.

Originally lived under :mod:`expert_work.runtime.context` (Stream A.2
batch 5); promoted to ``expert-work-common`` in Stream A.7 so the structured
logger / OTel SDK primitives can consume it without
``expert-work-common -> expert-work-runtime`` dependency reversal.

The auth middleware (Stream C.1) sets ``tenant_id`` after JWT validation.
The OTel SDK boundary (Stream A.8) sets ``trace_id``. Structured logger
and metric emitters read these to auto-inject context into every record.

**Asyncio semantics**: ``ContextVar`` is task-local, not thread-local.
Each FastAPI request runs in its own task → contexts are isolated.
``asyncio.create_task`` and ``asyncio.to_thread`` inherit the parent
task's context (usually the intended behaviour). If a background task
must NOT see the foreground context, wrap with
:func:`contextvars.copy_context`.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Final
from uuid import UUID

_current_tenant: Final[ContextVar[UUID | None]] = ContextVar(
    "expert_work_current_tenant",
    default=None,
)
_current_trace_id: Final[ContextVar[str | None]] = ContextVar(
    "expert_work_current_trace_id",
    default=None,
)
_current_run_id: Final[ContextVar[UUID | None]] = ContextVar(
    "expert_work_current_run_id",
    default=None,
)
_current_thread_id: Final[ContextVar[UUID | None]] = ContextVar(
    "expert_work_current_thread_id",
    default=None,
)


# ---------------------------------------------------------------------------
# tenant
# ---------------------------------------------------------------------------


def set_current_tenant(tenant_id: UUID) -> Token[UUID | None]:
    """Bind ``tenant_id`` to the current async task; return a reset token."""
    return _current_tenant.set(tenant_id)


def reset_current_tenant(token: Token[UUID | None]) -> None:
    """Restore the previous tenant context captured by ``token``."""
    _current_tenant.reset(token)


def get_current_tenant() -> UUID | None:
    """Return the bound tenant id, or ``None`` when unset.

    Use this in log / metric / trace emitters where unset tenant is a
    valid state (background jobs, healthchecks).
    """
    return _current_tenant.get()


def require_current_tenant() -> UUID:
    """Return the bound tenant id; raise :class:`RuntimeError` if unset.

    Use this from code paths that must not be invoked outside an
    auth-scoped request — the message names the offender so a stack
    trace localizes the bug.
    """
    tenant_id = _current_tenant.get()
    if tenant_id is None:
        msg = (
            "tenant context not set; this code path must run inside an "
            "auth-scoped request (set via set_current_tenant in auth middleware)"
        )
        raise RuntimeError(msg)
    return tenant_id


# ---------------------------------------------------------------------------
# trace_id
# ---------------------------------------------------------------------------


def set_current_trace_id(trace_id: str) -> Token[str | None]:
    """Bind ``trace_id`` (W3C ``traceparent`` ``trace-id`` portion)."""
    return _current_trace_id.set(trace_id)


def reset_current_trace_id(token: Token[str | None]) -> None:
    """Restore the previous trace_id captured by ``token``."""
    _current_trace_id.reset(token)


def get_current_trace_id() -> str | None:
    """Return the bound trace id, or ``None`` when unset."""
    return _current_trace_id.get()


# ---------------------------------------------------------------------------
# run_id — Stream HX-4 (Mini-ADR HX-D4)
# ---------------------------------------------------------------------------


def set_current_run_id(run_id: UUID) -> Token[UUID | None]:
    """Bind ``run_id`` to the current async task (the run worker binds at
    its entry; child tasks inherit). Return a reset token."""
    return _current_run_id.set(run_id)


def reset_current_run_id(token: Token[UUID | None]) -> None:
    """Restore the previous run_id captured by ``token``."""
    _current_run_id.reset(token)


def get_current_run_id() -> UUID | None:
    """Return the bound run id, or ``None`` outside a run worker."""
    return _current_run_id.get()


# ---------------------------------------------------------------------------
# thread_id — B-175 P2-PR4 (the run's conversation, bound with run_id)
# ---------------------------------------------------------------------------


def set_current_thread_id(thread_id: UUID) -> Token[UUID | None]:
    """Bind the run's ``thread_id`` (the run worker binds it with the run id).
    Return a reset token."""
    return _current_thread_id.set(thread_id)


def reset_current_thread_id(token: Token[UUID | None]) -> None:
    """Restore the previous thread_id captured by ``token``."""
    _current_thread_id.reset(token)


def get_current_thread_id() -> UUID | None:
    """Return the bound thread id, or ``None`` outside a run worker."""
    return _current_thread_id.get()
