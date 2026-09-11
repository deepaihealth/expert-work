"""B-45 —— 这个 job 进程也要装 RLS listener,并逐条声明会话的作用域。

此前 ``main.py`` 从不调 ``build_rls_sessionmaker``:listener 在本进程根本不
存在,RLS Detect 信号不会出现,生产 enforce 时也不会有任何征兆。三条钉子:

1. ``build_session_factory`` 必须经过 ``build_rls_sessionmaker``(用替身记录
   调用,而不是断言 listener 全局已装 —— 同一 pytest 进程里别的测试早把
   listener 装上了,那种断言恒绿)。
2. ``run_once`` 的每个会话都带作用域:跨租户的可归档分组扫描声明 bypass,
   每组的读 / 删作用到该组自己的租户;退出后两个 ContextVar 复原。
3. **归因必须真的落盘**:``rls_caller`` 是 ``extra=`` 结构化字段,
   ``logging.basicConfig`` 的默认 format 只渲染 message,字段全丢 —— 那就
   回到了 #1443 「信号打了、归因看不见」的老坑。这里过一遍本进程真正装的
   formatter,断言 JSON 里带得出归因。

跨 greenlet 找应用帧那一层(#1443 第二层)要真 ORM 调用才咬得住,钉在
``test_archive_job_integration.py::test_rls_detect_signal_is_attributable_in_this_process``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from event_log_archive_job import main as main_module
from event_log_archive_job.job import EventLogArchiveJob
from event_log_archive_job.settings import EventLogArchiveSettings
from expert_work.persistence import rls
from expert_work.persistence.rls import (
    _rls_after_begin,
    bypass_rls_var,
    current_tenant_id_var,
)

_RLS_LOGGER = "expert_work.persistence.rls"


# --------------------------------------------------------------- 钉子 1


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


# --------------------------------------------------------------- 钉子 2


class _Result:
    """The two ``Result`` bits the job touches: ``fetchall`` and row shape."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows


class _MappedRow:
    """``_fetch_group`` reads ``row._mapping``; nothing else."""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping


class _RecordingSessionFactory:
    """Records the RLS ContextVars at the moment each statement is executed.

    Doubles as its own async context manager and session — the job only ever
    calls ``async with self._sf() as session`` / ``session.execute`` /
    ``session.commit``.
    """

    def __init__(self, results: list[_Result]) -> None:
        self._results = results
        self.scopes: list[tuple[bool, UUID | None]] = []

    def __call__(self) -> _RecordingSessionFactory:
        return self

    async def __aenter__(self) -> _RecordingSessionFactory:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        self.scopes.append((bypass_rls_var.get(), current_tenant_id_var.get()))
        return self._results.pop(0)

    async def commit(self) -> None:
        return None


class _RecordingObjectStore:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def put(self, key: str, _data: bytes, *, content_type: str) -> None:
        assert content_type == "application/x-ndjson"
        self.keys.append(key)


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


async def test_run_once_declares_bypass_for_the_scan_and_tenant_scope_per_group(
    clean_rls_context: None,
) -> None:
    """Three statements, three declared scopes: the cross-tenant DISTINCT scan
    under bypass, then this group's fetch + delete scoped to its own tenant."""
    tenant = uuid4()
    thread = uuid4()
    month = datetime(2026, 1, 1, tzinfo=UTC)
    factory = _RecordingSessionFactory(
        [
            _Result([(str(tenant), str(thread), month)]),
            _Result([_MappedRow({"id": 1, "seq": 1, "payload": "{}", "created_at": month})]),
            _Result([(1,)]),
        ]
    )
    store = _RecordingObjectStore()
    outer_tenant = uuid4()
    token = current_tenant_id_var.set(outer_tenant)
    try:
        job = EventLogArchiveJob(
            db_session_factory=factory,  # type: ignore[arg-type]
            object_store=store,  # type: ignore[arg-type]
            archive_age_days=180,
            batch_size=10,
        )
        report = await job.run_once()

        assert report.archived_objects == 1
        assert report.archived_rows == 1
        assert store.keys == [f"event-log/{tenant}/2026/01/{thread}.jsonl"]
        # scan = declared cross-tenant; group read + delete = this tenant only.
        assert factory.scopes == [(True, None), (False, tenant), (False, tenant)]
        # Scope exit restores the caller's context exactly.
        assert bypass_rls_var.get() is False
        assert current_tenant_id_var.get() == outer_tenant
    finally:
        current_tenant_id_var.reset(token)


def test_run_once_opens_no_session_outside_a_declared_scope() -> None:
    """Every ``await`` in ``run_once`` must sit inside ``_bypass_rls`` or
    ``_tenant_scope`` — a pass added outside either block would silently
    regress B-45 (an undeclared session is exactly what the Detect signal is
    there to catch, and it would fail closed under enforcement)."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(EventLogArchiveJob.run_once))
    func = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef))
    scope_names = {"_bypass_rls", "_tenant_scope"}

    def declares_scope(node: ast.AST) -> bool:
        return isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", None) in scope_names
            for item in node.items
        )

    awaits_outside: list[str] = []

    def visit(node: ast.AST, *, guarded: bool) -> None:
        inside = guarded or declares_scope(node)
        if isinstance(node, ast.Await) and not inside:
            awaits_outside.append(ast.unparse(node))
        for child in ast.iter_child_nodes(node):
            visit(child, guarded=inside)

    visit(func, guarded=False)
    assert awaits_outside == []


# --------------------------------------------------------------- 钉子 3


def test_configure_logging_renders_the_attribution_extras(
    clean_rls_context: None,
) -> None:
    """The process's own logging setup must put ``rls_caller`` on the wire.

    ``logging.basicConfig`` (what this job used before) renders only the
    message, so the signal would land in Loki as a byte-identical
    ``rls.would_fail_closed`` line with no attribution at all — the same
    failure #1443 fixed one layer up.
    """
    import io

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    stream = io.StringIO()
    try:
        main_module.configure_logging(
            EventLogArchiveSettings(_env_file=None),  # type: ignore[call-arg]
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
    assert payload["service"] == "event_log_archive_job"
    caller = payload["rls_caller"]
    assert caller.endswith(" test_configure_logging_renders_the_attribution_extras"), caller
    assert "expert_work.persistence.rls" not in caller
