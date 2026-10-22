"""B-209 PR-2 —— parse-pool limits: a worker's memory ceiling (security review
S-M1) and the figure step's share of a document's time budget (review H3)."""

from __future__ import annotations

import concurrent.futures
import sys
import types
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from control_plane.knowledge import ingestion
from control_plane.knowledge.parsing import DocumentParseError

_INFINITY = -1
_RLIMIT_AS = 9


class _Resource(types.ModuleType):
    """Stand-in for :mod:`resource`: records ``setrlimit`` calls."""

    RLIM_INFINITY = _INFINITY
    RLIMIT_AS = _RLIMIT_AS

    def __init__(self, hard: int = _INFINITY, error: Exception | None = None) -> None:
        super().__init__("resource")
        self.hard = hard
        self.error = error
        self.set: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(self, which: int) -> tuple[int, int]:
        assert which == _RLIMIT_AS
        return _INFINITY, self.hard

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        if self.error is not None:
            raise self.error
        self.set.append((which, limits))


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Resource]:
    def install(**kwargs: Any) -> _Resource:
        fake = _Resource(**kwargs)
        monkeypatch.setitem(sys.modules, "resource", fake)
        monkeypatch.setattr(ingestion, "_on_linux", lambda: True)
        return fake

    return install


def test_a_worker_gets_the_configured_address_space_cap(linux: Callable[..., _Resource]) -> None:
    fake = linux()
    ingestion.limit_worker_memory(6 << 30)
    assert fake.set == [(_RLIMIT_AS, (6 << 30, _INFINITY))]


def test_the_cap_never_exceeds_an_existing_hard_limit(linux: Callable[..., _Resource]) -> None:
    fake = linux(hard=2 << 30)
    ingestion.limit_worker_memory(6 << 30)
    assert fake.set == [(_RLIMIT_AS, (2 << 30, 2 << 30))]


def test_no_cap_when_switched_off_or_off_linux(
    linux: Callable[..., _Resource], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = linux()
    ingestion.limit_worker_memory(0)
    monkeypatch.setattr(ingestion, "_on_linux", lambda: False)
    ingestion.limit_worker_memory(6 << 30)
    assert fake.set == []


def test_a_cap_the_kernel_refuses_does_not_stop_the_worker(
    linux: Callable[..., _Resource], caplog: pytest.LogCaptureFixture
) -> None:
    linux(error=ValueError("not allowed"))
    with caplog.at_level("WARNING"):
        ingestion.limit_worker_memory(6 << 30)
    assert "knowledge.parse_worker_memory_limit_failed" in caplog.text


def test_the_setting_reaches_the_worker_initializer(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    class _Pool:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setattr(ingestion, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(ingestion, "_parse_executor", None)
    ingestion.configure_parse_pool(workers=2, timeout_s=60, max_vm_mb=1536)
    try:
        ingestion._get_parse_executor()
    finally:
        monkeypatch.setattr(ingestion, "_parse_executor", None)
        ingestion.configure_parse_pool(workers=2, timeout_s=600)
    (kwargs,) = created
    assert kwargs["initializer"] is ingestion.limit_worker_memory
    assert kwargs["initargs"] == (1536 << 20,)


@pytest.fixture
def threads(monkeypatch: pytest.MonkeyPatch) -> Iterator[concurrent.futures.ThreadPoolExecutor]:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ingestion, "_parse_executor", pool)
    yield pool
    pool.shutdown(wait=True)


def _out_of_memory() -> str:
    raise MemoryError


async def test_a_parse_out_of_memory_fails_that_document_only(
    threads: concurrent.futures.ThreadPoolExecutor,
) -> None:
    with pytest.raises(DocumentParseError, match="more memory"):
        await ingestion._in_parse_pool("big.pdf", _out_of_memory)
    # The pool is kept: the worker survived its own MemoryError.
    assert ingestion._parse_executor is threads


async def test_the_figure_step_spends_the_documents_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review H3: the figure collection's time comes out of the document's
    # budget, and it never gets more than the document has left.
    seen: list[float] = []

    async def fake_pool(
        filename: str, fn: Callable[..., Any], *args: object, budget: Any = None
    ) -> Any:
        seen.append(budget.remaining)
        budget.charge(fn())
        return None

    monkeypatch.setattr(ingestion, "_in_parse_pool", fake_pool)
    monkeypatch.setattr(ingestion, "_parse_timeout_s", 100.0)
    run, run_figures = ingestion._pool_runners("a.pdf")
    await run_figures(lambda: 10.0)
    await run(lambda: 85.0)
    await run_figures(lambda: 0.0)
    assert seen == [25.0, 90.0, 5.0]
