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
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from expert_work.persistence.rls import (
    _rls_after_begin,
    bypass_rls_var,
    current_tenant_id_var,
)

_LOGGER_NAME = "expert_work.persistence.rls"


@pytest.fixture(autouse=True)
def reset_context() -> Iterator[None]:
    """Each test gets a clean ContextVar state."""
    token_b = bypass_rls_var.set(False)
    token_t = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        bypass_rls_var.reset(token_b)
        current_tenant_id_var.reset(token_t)


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
