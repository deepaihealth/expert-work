"""Row-level security session wiring — Stream C.4.

Postgres RLS policies (migration ``0005_rls_baseline``) compare
``tenant_id`` against ``current_setting('app.tenant_id', true)::uuid``.
The application code must set that GUC variable on every transaction
**before** the first ``SELECT``/``INSERT`` runs, otherwise the policy
denies everything (``current_setting(..., true)`` returns ``''`` →
``::uuid`` cast errors out → policy evaluates to ``false``).

Design intent (STREAM-C-DESIGN § 2.6):

* The tenant id is carried by a :class:`~contextvars.ContextVar` set
  by :class:`control_plane.tenancy.RLSContextMiddleware`. ContextVar
  is inherited by asyncio tasks spawned within the request scope, so
  it flows correctly through SQLAlchemy's awaited operations.

* :func:`build_rls_sessionmaker` is the public entry point that opts
  a given :class:`sqlalchemy.ext.asyncio.async_sessionmaker` into the
  RLS wiring. Idempotent — calling it twice is a no-op. Under the
  hood the listener is attached to the base :class:`sqlalchemy.orm.Session`
  class so every session participates; the listener itself is a
  no-op when the ContextVar is unset, which keeps tests that don't
  care about RLS unaffected.

* The wrapped factory is otherwise indistinguishable from the
  original: existing SQL stores keep their ``async with self._sf()
  as session`` pattern with no code changes. RLS is transparent to
  the store layer.

PgBouncer compatibility:

* ``SET LOCAL`` is bound to the current transaction and reset on
  ``COMMIT`` / ``ROLLBACK``. Transaction-mode pooling is therefore
  safe — the server-side connection returned to PgBouncer carries no
  residual ``app.tenant_id`` value.

* ``set_config(name, value, is_local=true)`` is used rather than
  ``SET LOCAL app.tenant_id = '<uuid>'`` because the former accepts
  bind parameters; the latter cannot use placeholders, which would
  require building SQL by string interpolation and a separate UUID
  validator.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from types import FrameType
from typing import Final
from uuid import UUID

# greenlet is what ``sqlalchemy[asyncio]`` runs every sync ORM call on; it
# ships no type stubs (SQLAlchemy itself imports it under a local Protocol).
from greenlet import getcurrent  # type: ignore[import-untyped]
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

logger = logging.getLogger("expert_work.persistence.rls")

__all__ = [
    "RLS_GUC_NAME",
    "RLS_USER_GUC_NAME",
    "build_rls_sessionmaker",
    "bypass_rls_var",
    "current_tenant_id_var",
    "current_user_id_var",
]


RLS_GUC_NAME: Final[str] = "app.tenant_id"

#: Stream J.3 — the user-level GUC. Per-user data tables (memory_item,
#: and later workspace / artifact) carry a ``user_id`` predicate on top
#: of tenant isolation (Mini-ADR J-1, defence in depth).
RLS_USER_GUC_NAME: Final[str] = "app.user_id"

# Set by the per-request middleware; cleared on response. Default
# ``None`` means "no tenant scoped", which makes every read return
# zero rows under RLS — that's the desired fail-closed behaviour.
current_tenant_id_var: ContextVar[UUID | None] = ContextVar(
    "expert_work.rls.tenant_id",
    default=None,
)

# Stream J.3 — set alongside the tenant var for per-user data access.
# ``None`` → ``app.user_id`` is not emitted; a query against a
# user-scoped table (memory_item) then sees zero rows (fail-closed).
current_user_id_var: ContextVar[UUID | None] = ContextVar(
    "expert_work.rls.user_id",
    default=None,
)

# Explicit opt-out for admin paths that want to ``SET ROLE`` to a
# BYPASSRLS role (e.g. ``audit_reader``). When ``True``, the
# ``after_begin`` listener skips emitting ``set_config`` so the admin
# session can manage its own role with no interference.
bypass_rls_var: ContextVar[bool] = ContextVar(
    "expert_work.rls.bypass",
    default=False,
)

# Module-level flag: do we have the listener attached to ``Session``?
# A list (not a bool) so it works under hot reloads / test imports
# where the module body is re-evaluated but the SQLAlchemy event
# registry survives.
_LISTENER_INSTALLED: list[bool] = []

# ---------------------------------------------------------------------------
# Phase-1 Detect signal attribution (``rls.would_fail_closed``).
#
# The signal is only useful if each occurrence names the code path that
# opened a session with neither tenant context nor explicit bypass. Two
# things defeated the original ``stack_info=True`` attempt:
#
# * The JSON formatter (expert_work.common.observability.log) drops
#   ``stack_info`` as a reserved LogRecord attribute, so production logs
#   carried a byte-identical message and nothing else.
# * Even in a plain-text sink the stack would have been useless: the ORM
#   runs every sync call inside a *greenlet* (``greenlet_spawn``), and a
#   greenlet's frame chain stops at its own entry point. From inside the
#   listener, ``sys._getframe()`` only ever sees SQLAlchemy frames. The
#   application frames live on the **parent** greenlet's suspended stack.
#
# So: walk the current stack, then continue into the parent greenlet's
# ``gr_frame`` chain (and its parents'), skip the transport layers that sit
# on every path, and report the first frame outside the store layer as
# ``rls_caller`` (the code that owns the tenant context) plus the next
# frame as ``rls_caller_outer``.
# ---------------------------------------------------------------------------

#: Module families between the listener and the application frame on every
#: path: ORM/engine machinery, the greenlet bridge, asyncio task plumbing,
#: contextmanager wrappers. Never the attributable owner of a session.
_TRANSPORT_MODULES: Final[frozenset[str]] = frozenset(
    {"sqlalchemy", "greenlet", "asyncio", "contextlib", "concurrent", "threading"}
)

#: SQL stores are mechanism, not policy — the tenant context is set (or the
#: bypass declared) by whoever calls them. Attribution skips past the store
#: layer (this package, and the runtime library's stores such as
#: ``expert_work.runtime.event_log.db`` / ``runs.store``) to that caller; a
#: store frame is only used as a fallback when the whole chain is store layer.
_STORE_LAYER_PREFIXES: Final[tuple[str, ...]] = ("expert_work.persistence.", "expert_work.runtime.")

#: Per-caller rate limit for the signal: the first occurrence of a caller
#: always logs; beyond ``_SIGNAL_MAX_PER_WINDOW`` records in a window the
#: rest are dropped and the drop count rides on the next emitted record as
#: ``rls_suppressed``. Bounds a hot loop to a few hundred bytes a minute
#: instead of a line per transaction.
_SIGNAL_MAX_PER_WINDOW: int = 10
_SIGNAL_WINDOW_S: float = 60.0
_signal_lock = threading.Lock()
_signal_state: dict[str, tuple[float, int]] = {}
_monotonic = time.monotonic


def _iter_frames() -> Iterator[FrameType]:
    """Yield frames from the current stack outward, then from each parent
    greenlet's suspended stack — the only way to reach the ``await``-side
    application frames from inside a ``greenlet_spawn``'d ORM call."""
    frame: FrameType | None = sys._getframe()
    while frame is not None:
        yield frame
        frame = frame.f_back
    parent = getcurrent().parent
    while parent is not None:
        frame = parent.gr_frame
        while frame is not None:
            yield frame
            frame = frame.f_back
        parent = parent.parent


def _module_of(frame: FrameType) -> str:
    name = frame.f_globals.get("__name__")
    return name if isinstance(name, str) else frame.f_code.co_filename


def _is_transport(frame: FrameType) -> bool:
    module = _module_of(frame)
    return module == __name__ or module.partition(".")[0] in _TRANSPORT_MODULES


def _describe(frame: FrameType) -> str:
    return f"{_module_of(frame)}:{frame.f_lineno} {frame.f_code.co_name}"


def _locate_caller() -> tuple[str, str | None]:
    """Return ``(rls_caller, rls_caller_outer)`` for the current session.

    ``rls_caller`` is the innermost non-transport frame outside the store
    layer (falling back to the innermost non-transport frame at all);
    ``rls_caller_outer`` is the next non-transport frame above it.
    """
    frames = (frame for frame in _iter_frames() if not _is_transport(frame))
    fallback: FrameType | None = None
    for frame in frames:
        if fallback is None:
            fallback = frame
        if not _module_of(frame).startswith(_STORE_LAYER_PREFIXES):
            outer = next(frames, None)
            return _describe(frame), None if outer is None else _describe(outer)
    if fallback is None:
        return "<unknown>", None
    return _describe(fallback), None


def _should_emit(caller: str, now: float) -> tuple[bool, int]:
    """Apply the per-caller window; returns ``(emit, suppressed_last_window)``."""
    with _signal_lock:
        start, count = _signal_state.get(caller, (now, 0))
        suppressed = 0
        if now - start >= _SIGNAL_WINDOW_S:
            suppressed = max(0, count - _SIGNAL_MAX_PER_WINDOW)
            start, count = now, 0
        count += 1
        _signal_state[caller] = (start, count)
        return count <= _SIGNAL_MAX_PER_WINDOW, suppressed


def _reset_signal_state() -> None:
    """Test hook — forget every caller's window."""
    with _signal_lock:
        _signal_state.clear()


def _emit_would_fail_closed() -> None:
    caller, outer = _locate_caller()
    emit, suppressed = _should_emit(caller, _monotonic())
    if not emit:
        return
    logger.warning(
        "rls.would_fail_closed",
        extra={
            "rls_caller": caller,
            "rls_caller_outer": outer,
            "rls_suppressed": suppressed,
        },
    )


def _emit_set_config(connection: Connection, name: str, value: str) -> None:
    """Run ``SELECT set_config(name, value, true)`` on ``connection``."""
    connection.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": name, "value": value},
    )


def _rls_after_begin(
    _session: Session,
    _transaction: SessionTransaction,
    connection: Connection,
) -> None:
    """``after_begin`` listener — emits ``SET LOCAL`` from the ContextVars.

    Emits ``app.tenant_id`` and — for per-user data tables (Stream J.3)
    — ``app.user_id``. An unset var means the GUC is not emitted, so
    RLS on that axis fails closed (``NULLIF→NULL`` → row denied).
    """
    if bypass_rls_var.get():
        return
    tenant_id = current_tenant_id_var.get()
    if tenant_id is None:
        # Non-bypass session with no tenant context: under RLS enforcement this
        # fail-closes (zero rows). Phase-1 Detect signal — surfaces every path
        # that is neither tenant-scoped nor explicit-bypass so it can be fixed
        # BEFORE enforcement lands. No behavior change here. Attribution
        # (``rls_caller`` / ``rls_caller_outer``) and rate limiting live in
        # ``_emit_would_fail_closed`` — see the block comment above it.
        _emit_would_fail_closed()
    if tenant_id is not None:
        _emit_set_config(connection, RLS_GUC_NAME, str(tenant_id))
    user_id = current_user_id_var.get()
    if user_id is not None:
        _emit_set_config(connection, RLS_USER_GUC_NAME, str(user_id))


def _install_listener_once() -> None:
    if _LISTENER_INSTALLED:
        return
    if event.contains(Session, "after_begin", _rls_after_begin):
        # Defensive: another import path already attached us.
        _LISTENER_INSTALLED.append(True)
        return
    event.listen(Session, "after_begin", _rls_after_begin)
    _LISTENER_INSTALLED.append(True)


def build_rls_sessionmaker(
    base: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Opt ``base`` into the RLS ``after_begin`` listener (idempotent).

    The listener is attached at module level on the global
    :class:`sqlalchemy.orm.Session` class — every ``async_sessionmaker``
    in the process therefore participates automatically once any
    factory has been wrapped. The function still exists so callers
    declare intent explicitly and so tests can build factories that
    are *not* wrapped if they need to bypass RLS at the SQLAlchemy
    layer (e.g. low-level migration helpers).
    """
    _install_listener_once()
    return base
