"""Detect: a non-bypass session with no tenant context is logged (Phase 1).

RLS is currently inert at runtime (the app connects as a DB superuser);
before enforcement lands (a later task adds ``SET LOCAL ROLE app_user``),
this pins the Phase-1 **Detect** signal: ``_rls_after_begin`` logs a
structured ``rls.would_fail_closed`` WARNING whenever a session is
neither bypassed nor tenant-scoped. Under future enforcement that
session would fail closed (zero rows) — the warning surfaces every such
path so it can be fixed before cutover. No behaviour change here.

This exercises the listener directly with a stub connection, in the same
style as ``test_rls_unit.py`` — no database needed. (The task brief's
sketch used ``sqlite+aiosqlite``, but ``aiosqlite`` is not a dependency
of this repo — confirmed absent from ``pyproject.toml``/``uv.lock``.)

Attribution (the ``rls_caller`` / ``rls_caller_outer`` extras) is pinned
here too: the signal must name an application frame — never ``rls.py``
itself, never SQLAlchemy — and must still do so when the listener runs
inside the ORM's greenlet, where the plain frame chain is cut off from
the awaiting application code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.util import greenlet_spawn

from expert_work.persistence import rls
from expert_work.persistence.rls import (
    _rls_after_begin,
    bypass_rls_var,
    current_tenant_id_var,
)

_LOGGER_NAME = "expert_work.persistence.rls"
_SIGNAL = "rls.would_fail_closed"


@pytest.fixture(autouse=True)
def reset_context() -> Iterator[None]:
    """Each test gets a clean ContextVar state and a fresh rate-limit window."""
    token_b = bypass_rls_var.set(False)
    token_t = current_tenant_id_var.set(None)
    rls._reset_signal_state()
    try:
        yield
    finally:
        rls._reset_signal_state()
        bypass_rls_var.reset(token_b)
        current_tenant_id_var.reset(token_t)


def _signal_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER_NAME and r.message == _SIGNAL]


def _fire_listener() -> None:
    _rls_after_begin(MagicMock(), MagicMock(), MagicMock())


def test_detects_would_fail_closed_when_no_tenant_and_not_bypass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-bypass + no tenant context: the Phase-1 signal fires; no GUC set."""
    bypass_rls_var.set(False)
    current_tenant_id_var.set(None)
    connection = MagicMock()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _rls_after_begin(MagicMock(), MagicMock(), connection)

    assert any("rls.would_fail_closed" in r.message for r in caplog.records)
    connection.execute.assert_not_called()


def test_no_warning_when_bypass_set(caplog: pytest.LogCaptureFixture) -> None:
    """Explicit bypass: no signal, no GUC — admin paths stay unaffected."""
    bypass_rls_var.set(True)
    current_tenant_id_var.set(None)
    connection = MagicMock()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _rls_after_begin(MagicMock(), MagicMock(), connection)

    # 只看本模块自己的 logger:caplog 抓的是所有冒泡上来的记录,全局零
    # WARNING 断言会被无关噪音污染(#1077:CI 里其他测试遗留的 otel
    # exporter 后台线程往 root 吐 retry WARNING,卡红过一个 deploy-only PR)。
    assert [r.message for r in caplog.records if r.name == _LOGGER_NAME] == []
    connection.execute.assert_not_called()


def test_no_warning_when_tenant_set(caplog: pytest.LogCaptureFixture) -> None:
    """Tenant scoped: no signal, and the GUC is emitted as before."""
    bypass_rls_var.set(False)
    current_tenant_id_var.set(uuid4())
    connection = MagicMock()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _rls_after_begin(MagicMock(), MagicMock(), connection)

    assert [r.message for r in caplog.records if r.name == _LOGGER_NAME] == []
    connection.execute.assert_called_once()


# --- attribution -----------------------------------------------------------


def test_signal_names_the_application_frame_not_rls_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``rls_caller`` points at the code that opened the session — this test
    function — not at ``rls.py`` (which always sits at the top of the stack)
    and not at a stdlib/transport frame."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _rls_after_begin(MagicMock(), MagicMock(), MagicMock())

    (record,) = _signal_records(caplog)
    caller = record.__dict__["rls_caller"]
    assert caller.startswith(f"{__name__}:")
    assert caller.endswith(" test_signal_names_the_application_frame_not_rls_itself")
    assert "expert_work.persistence.rls" not in caller
    # The next frame up is reported too, so a shared helper is still traceable.
    outer = record.__dict__["rls_caller_outer"]
    assert outer is not None
    assert "expert_work.persistence.rls" not in outer


async def test_signal_attributes_across_the_greenlet_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ORM runs the listener inside ``greenlet_spawn``; the plain frame
    chain there ends at SQLAlchemy. Attribution must reach the awaiting
    application frame on the parent greenlet's stack."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await greenlet_spawn(_rls_after_begin, MagicMock(), MagicMock(), MagicMock())

    (record,) = _signal_records(caplog)
    caller = record.__dict__["rls_caller"]
    assert caller.endswith(" test_signal_attributes_across_the_greenlet_boundary"), caller
    assert "sqlalchemy" not in caller


def test_signal_skips_store_layer_frames(caplog: pytest.LogCaptureFixture) -> None:
    """A store method (``expert_work.persistence.*``) is mechanism; the owner
    of the tenant context is whoever called it. Attribution reports that
    caller, and the store frame is not mistaken for it."""
    # ``exec`` only so the frame's ``__name__`` reads as a persistence module.
    namespace: dict[str, object] = {"__name__": "expert_work.persistence.fake_store.sql"}
    exec("def store_method(listener):\n    listener(None, None, None)\n", namespace)  # noqa: S102
    store_method = namespace["store_method"]
    assert callable(store_method)
    typed_store_method: Callable[[Callable[..., None]], None] = store_method

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        typed_store_method(_rls_after_begin)

    (record,) = _signal_records(caplog)
    caller = record.__dict__["rls_caller"]
    assert caller.endswith(" test_signal_skips_store_layer_frames"), caller
    assert not caller.startswith("expert_work.persistence.")


# --- rate limit ------------------------------------------------------------


def test_first_occurrence_always_logs_then_window_caps(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per caller: the first record is never dropped; past the cap the rest of
    the window is dropped; a new window logs again and reports how many were
    dropped."""
    clock = [1000.0]

    def fake_monotonic() -> float:
        return clock[0]

    monkeypatch.setattr(rls, "_monotonic", fake_monotonic)
    cap = rls._SIGNAL_MAX_PER_WINDOW

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        for _ in range(cap + 3):
            _fire_listener()
        first_window = _signal_records(caplog)
        clock[0] += rls._SIGNAL_WINDOW_S
        _fire_listener()
        after_roll = _signal_records(caplog)[len(first_window) :]

    assert len(first_window) == cap
    assert first_window[0].__dict__["rls_suppressed"] == 0
    (rolled,) = after_roll
    assert rolled.__dict__["rls_suppressed"] == 3


def test_rate_limit_is_per_caller(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausting one caller's window must not silence a different caller's
    first occurrence."""

    def frozen_monotonic() -> float:
        return 1000.0

    monkeypatch.setattr(rls, "_monotonic", frozen_monotonic)
    monkeypatch.setattr(rls, "_SIGNAL_MAX_PER_WINDOW", 1)

    def other_call_site() -> None:
        _rls_after_begin(MagicMock(), MagicMock(), MagicMock())

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _fire_listener()
        _fire_listener()  # same caller (the helper) — dropped
        other_call_site()  # new caller — its first, must log

    callers = [r.__dict__["rls_caller"] for r in _signal_records(caplog)]
    assert len(callers) == 2
    assert callers[0].endswith(" _fire_listener")
    assert callers[1].endswith(" other_call_site")
