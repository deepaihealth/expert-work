"""B-45 —— supervisor 进程也要装 RLS listener,并且归因要真的落盘。

此前 ``app.py`` 从不调 ``build_rls_sessionmaker``:listener 在本进程根本不
存在,RLS Detect 信号不会出现,生产 enforce 时也不会有任何征兆。三条钉子:

1. ``build_session_factory`` 必须经过 ``build_rls_sessionmaker``(用替身记录
   调用,而不是断言 listener 全局已装 —— 同一 pytest 进程里别的测试早把
   listener 装上了,那种断言恒绿)。
2. lifespan 必须走它,不能再直接 ``create_async_session_factory``。
3. **归因必须真的落盘**:``rls_caller`` 是 ``extra=`` 结构化字段;uvicorn 只给
   ``uvicorn.*`` 配 handler,root 上没有,``expert_work.*`` 的 WARNING 落到
   ``logging.lastResort`` —— 它只打 message,归因字段一个不留。那就回到了
   #1443「信号打了、归因看不见」的老坑。

与两个 job 不同,supervisor **只装 listener,不代替任何路径声明作用域** ——
它的会话形状是三分的(带租户的前门请求 / 只拿到 ``sandbox_id`` 的
release·destroy·exec / reaper·池补货·每日备份这些跨租户常驻循环),逐条定作用
域是 RLS 第二轮判定的事。真 ORM 调用里的归因(#1443 第二层,跨 greenlet 找
应用帧)钉在 ``test_db_store_backend_scope.py`` 的集成测里。
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import logging
import textwrap
from collections.abc import Iterator

import pytest

from expert_work.persistence import rls
from expert_work.persistence.rls import (
    _rls_after_begin,
    bypass_rls_var,
    current_tenant_id_var,
)
from sandbox_supervisor import app as app_module
from sandbox_supervisor.settings import SandboxSupervisorSettings

_RLS_LOGGER = "expert_work.persistence.rls"


@pytest.fixture
def clean_rls_context() -> Iterator[None]:
    token_b = bypass_rls_var.set(False)
    token_t = current_tenant_id_var.set(None)
    rls._reset_signal_state()
    try:
        yield
    finally:
        rls._reset_signal_state()
        current_tenant_id_var.reset(token_t)
        bypass_rls_var.reset(token_b)


def test_build_session_factory_goes_through_build_rls_sessionmaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    sentinel = object()

    def _fake_build(base: object) -> object:
        seen.append(base)
        return sentinel

    def _fake_base_factory(engine: object) -> object:
        return ("base", engine)

    monkeypatch.setattr(app_module, "build_rls_sessionmaker", _fake_build)
    monkeypatch.setattr(app_module, "create_async_session_factory", _fake_base_factory)

    out = app_module.build_session_factory("engine")  # type: ignore[arg-type]

    assert out is sentinel
    assert seen == [("base", "engine")]


def test_lifespan_builds_its_factory_through_build_session_factory() -> None:
    """The production lifespan must not reach for the bare factory again — a
    second, unwrapped ``create_async_session_factory`` call there would leave
    part of the process outside the RLS listener with nothing to show it."""
    src = textwrap.dedent(inspect.getsource(app_module.create_app))
    tree = ast.parse(src)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_session_factory" in called
    assert "create_async_session_factory" not in called


def test_configure_logging_renders_the_attribution_extras(clean_rls_context: None) -> None:
    """The process's own logging setup must put ``rls_caller`` on the wire.

    Without ``init_logging`` the signal lands as a byte-identical
    ``rls.would_fail_closed`` line with no attribution at all — attaching the
    listener would then buy nothing.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    stream = io.StringIO()
    try:
        app_module.configure_logging(
            SandboxSupervisorSettings(_env_file=None),  # type: ignore[call-arg]
            stream=stream,
        )
        # Fire the real listener: the extras must come from rls.py, not the test.
        _rls_after_begin(object(), object(), object())  # type: ignore[arg-type]
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines, "nothing reached the configured stream"
    payload = json.loads(lines[-1])
    assert payload["message"] == "rls.would_fail_closed"
    assert payload["logger"] == _RLS_LOGGER
    assert payload["service"] == "sandbox_supervisor"
    caller = payload["rls_caller"]
    assert caller.endswith(" test_configure_logging_renders_the_attribution_extras"), caller
    assert "expert_work.persistence.rls" not in caller
