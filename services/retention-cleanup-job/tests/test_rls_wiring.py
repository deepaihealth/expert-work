"""B-45 —— 这个 job 进程也要装 RLS listener,并显式声明跨租户 bypass。

此前 ``main.py`` 从不调 ``build_rls_sessionmaker``:listener 在本进程根本不
存在,RLS Detect 信号不会出现,生产 enforce 时也不会有任何征兆。三条钉子:

1. ``build_session_factory`` 必须经过 ``build_rls_sessionmaker``(用替身记录
   调用,而不是断言 listener 全局已装——同一 pytest 进程里别的测试早把
   listener 装上了,那种断言恒绿)。
2. ``run_once`` 里每个 store 调用都在 bypass 作用域内,退出后两个 ContextVar
   复原。
3. **归因必须真的落盘**:``rls_caller`` 是 ``extra=`` 结构化字段,
   ``logging.basicConfig``(这个 job 此前用的)的默认 format 只渲染 message,
   字段全丢 —— #1493 在另外两个进程修掉的第二层失效,这里是第三个。而这个
   job 是三个里**唯一真部署的**(test overlay CronJob 已起),所以今天落
   Loki 的就是无归因那条。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from expert_work.persistence import rls
from expert_work.persistence.rls import (
    _rls_after_begin,
    bypass_rls_var,
    current_tenant_id_var,
)
from retention_cleanup_job import main as main_module
from retention_cleanup_job.job import RetentionCleanupJob, _bypass_rls
from retention_cleanup_job.settings import RetentionCleanupSettings

_RLS_LOGGER = "expert_work.persistence.rls"


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

    monkeypatch.setattr(main_module, "build_rls_sessionmaker", _fake_build)
    monkeypatch.setattr(main_module, "create_async_session_factory", _fake_base_factory)

    out = main_module.build_session_factory("engine")  # type: ignore[arg-type]

    assert out is sentinel
    assert seen == [("base", "engine")]


class _RecordingMemoryStore:
    """Only the one method ``_sweep_memory`` calls; records the RLS ContextVars
    at the moment the sweep calls into it."""

    def __init__(self) -> None:
        self.scopes: list[tuple[bool, object]] = []

    async def hard_delete_expired(self, *, before: datetime, limit: int) -> int:
        self.scopes.append((bypass_rls_var.get(), current_tenant_id_var.get()))
        return 1


@pytest.mark.asyncio
async def test_bypass_scope_encloses_store_calls_and_restores_context() -> None:
    store = _RecordingMemoryStore()
    outer_tenant = uuid4()
    token = current_tenant_id_var.set(outer_tenant)
    try:
        job = RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            memory_store=store,  # type: ignore[arg-type]
        )
        with _bypass_rls():
            assert await job._sweep_memory() == 1
        assert store.scopes == [(True, None)]
        # Scope exit restores the caller's context exactly.
        assert bypass_rls_var.get() is False
        assert current_tenant_id_var.get() == outer_tenant
    finally:
        current_tenant_id_var.reset(token)


def test_run_once_wraps_every_pass_in_bypass() -> None:
    """The bypass scope must enclose the whole pass list in ``run_once`` —
    a pass added outside the ``with`` block would silently regress B-45."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(RetentionCleanupJob.run_once))
    func = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef))
    awaits_outside: list[str] = []
    for node in func.body:
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", None) == "_bypass_rls"
            for item in node.items
        ):
            continue
        awaits_outside.extend(ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Await))
    assert awaits_outside == []


# ------------------------------------------------ 审查补:启动时校验 workspace_root


def test_resolve_workspace_root_none_is_a_warning_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = RetentionCleanupSettings(_env_file=None, workspace_root=None)  # type: ignore[call-arg]
    with caplog.at_level("WARNING"):
        assert main_module.resolve_workspace_root(settings, environ={}) is None
    assert any("workspace_root_unset" in r.message for r in caplog.records)


def test_resolve_workspace_root_returns_a_validated_root(tmp_path: Path) -> None:
    settings = RetentionCleanupSettings(_env_file=None, workspace_root=str(tmp_path))  # type: ignore[call-arg]
    environ = {"EXPERT_WORK_WORKSPACE_NAS_ROOT": str(tmp_path)}
    assert main_module.resolve_workspace_root(settings, environ=environ) == str(tmp_path.resolve())


def test_resolve_workspace_root_refuses_to_run_on_control_plane_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """envFrom 带进来的 control-plane 键(EXPERT_WORK_WORKSPACE_NAS_ROOT)与 job 自己
    的根不一致 → 整个 job 拒跑:两边看的不是同一棵树,删的就不是登记的那份文件。"""
    settings = RetentionCleanupSettings(_env_file=None, workspace_root=str(tmp_path))  # type: ignore[call-arg]
    environ = {"EXPERT_WORK_WORKSPACE_NAS_ROOT": "/somewhere/else"}
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        main_module.resolve_workspace_root(settings, environ=environ)
    assert any("workspace_root_mismatch" in r.message for r in caplog.records)


def test_resolve_workspace_root_refuses_symlink_or_missing_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    for bad in (str(link), str(tmp_path / "missing")):
        settings = RetentionCleanupSettings(_env_file=None, workspace_root=bad)  # type: ignore[call-arg]
        with caplog.at_level("ERROR"), pytest.raises(SystemExit):
            main_module.resolve_workspace_root(settings, environ={})
    assert sum("workspace_root_invalid" in r.message for r in caplog.records) == 2


# --------------------------------------------------------------- 钉子 3


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


def test_configure_logging_renders_the_attribution_extras(
    clean_rls_context: None,
) -> None:
    """The process's own logging setup must put ``rls_caller`` on the wire.

    ``logging.basicConfig`` (what this job used before) renders only the
    message, so the signal would land in Loki as a byte-identical
    ``rls.would_fail_closed`` line with no attribution at all — the same
    failure #1443 fixed one layer up and #1493 fixed in the other two
    processes.
    """
    import io

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    stream = io.StringIO()
    try:
        main_module.configure_logging(
            RetentionCleanupSettings(_env_file=None),  # type: ignore[call-arg]
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
    assert payload["service"] == "retention_cleanup_job"
    caller = payload["rls_caller"]
    assert caller.endswith(" test_configure_logging_renders_the_attribution_extras"), caller
    assert "expert_work.persistence.rls" not in caller
