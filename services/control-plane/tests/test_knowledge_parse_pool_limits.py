"""B-209 PR-2 —— parse-pool limits: a worker's memory ceiling (security review
S-M1), the figure step's share of a document's time budget (review H3), and
the figure step's own pool (re-review M2)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import types
from collections.abc import Callable, Iterator
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import pytest

from control_plane.knowledge import ingestion, parse_workers
from control_plane.knowledge.parse_workers import FIGURE_POOL, PoolBudget
from control_plane.knowledge.parsing import DocumentParseError

_INFINITY = -1
_RLIMIT_AS = 9


class _Resource(types.ModuleType):
    """Stand-in for :mod:`resource`: records ``setrlimit`` calls."""

    RLIM_INFINITY = _INFINITY
    RLIMIT_AS = _RLIMIT_AS

    def __init__(
        self, soft: int = _INFINITY, hard: int = _INFINITY, error: Exception | None = None
    ) -> None:
        super().__init__("resource")
        self.soft = soft
        self.hard = hard
        self.error = error
        self.set: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(self, which: int) -> tuple[int, int]:
        assert which == _RLIMIT_AS
        return self.soft, self.hard

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        if self.error is not None:
            raise self.error
        self.set.append((which, limits))


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Resource]:
    def install(**kwargs: Any) -> _Resource:
        fake = _Resource(**kwargs)
        monkeypatch.setitem(sys.modules, "resource", fake)
        monkeypatch.setattr(parse_workers, "_on_linux", lambda: True)
        return fake

    return install


def test_a_worker_gets_the_configured_address_space_cap(linux: Callable[..., _Resource]) -> None:
    fake = linux()
    parse_workers.limit_worker_memory(6 << 30)
    assert fake.set == [(_RLIMIT_AS, (6 << 30, _INFINITY))]


def test_the_cap_never_exceeds_an_existing_hard_limit(linux: Callable[..., _Resource]) -> None:
    fake = linux(hard=2 << 30)
    parse_workers.limit_worker_memory(6 << 30)
    assert fake.set == [(_RLIMIT_AS, (2 << 30, 2 << 30))]


def test_the_cap_never_raises_an_existing_soft_limit(linux: Callable[..., _Resource]) -> None:
    # Re-review L4: a lower soft limit already set (by the container) stays.
    fake = linux(soft=3 << 30, hard=8 << 30)
    parse_workers.limit_worker_memory(6 << 30)
    assert fake.set == [(_RLIMIT_AS, (3 << 30, 8 << 30))]


def test_no_cap_when_switched_off_or_off_linux(
    linux: Callable[..., _Resource], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = linux()
    parse_workers.limit_worker_memory(0)
    monkeypatch.setattr(parse_workers, "_on_linux", lambda: False)
    parse_workers.limit_worker_memory(6 << 30)
    assert fake.set == []


def test_a_cap_the_kernel_refuses_does_not_stop_the_worker(
    linux: Callable[..., _Resource], caplog: pytest.LogCaptureFixture
) -> None:
    linux(error=ValueError("not allowed"))
    with caplog.at_level("WARNING"):
        parse_workers.limit_worker_memory(6 << 30)
    assert "knowledge.parse_worker_memory_limit_failed" in caplog.text


def test_the_setting_reaches_both_pools_worker_initializers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []

    class _Pool:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setattr(ingestion, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(parse_workers, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(ingestion, "_parse_executor", None)
    monkeypatch.setattr(FIGURE_POOL, "_executor", None)
    ingestion.configure_parse_pool(workers=2, timeout_s=60, max_vm_mb=1536)
    try:
        ingestion._get_parse_executor()
        FIGURE_POOL._get_executor()
    finally:
        monkeypatch.setattr(ingestion, "_parse_executor", None)
        monkeypatch.setattr(FIGURE_POOL, "_executor", None)
        ingestion.configure_parse_pool(workers=2, timeout_s=600)
    main, figures = created
    for kwargs in (main, figures):
        assert kwargs["initializer"] is parse_workers.limit_worker_memory
        assert kwargs["initargs"] == (1536 << 20,)
    assert (main["max_workers"], figures["max_workers"]) == (2, 1)


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
    seen: list[tuple[str, float]] = []

    async def fake_pool(
        filename: str, fn: Callable[..., Any], *args: object, budget: Any = None
    ) -> Any:
        seen.append(("parse", budget.remaining))
        budget.charge(fn())

    async def fake_figures(
        filename: str, fn: Callable[..., Any], *args: object, budget: Any = None
    ) -> Any:
        seen.append(("figures", budget.remaining))
        budget.charge(fn())

    monkeypatch.setattr(ingestion, "_in_parse_pool", fake_pool)
    monkeypatch.setattr(FIGURE_POOL, "run", fake_figures)
    monkeypatch.setattr(ingestion, "_parse_timeout_s", 100.0)
    run, run_figures = ingestion._pool_runners("a.pdf")
    await run_figures(lambda: 10.0)
    await run(lambda: 85.0)
    await run_figures(lambda: 0.0)
    assert seen == [("figures", 25.0), ("parse", 90.0), ("figures", 5.0)]


class _Process:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class _Executor(concurrent.futures.Executor):
    """Executor double: ``mode`` "never" (a call that never returns), "broken"
    (``submit`` raises ``BrokenProcessPool``) or "ok" (returns ``"done"``)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._processes = {1: _Process()}
        self.shut_down = False

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        if self.mode == "broken":
            raise BrokenProcessPool("a child process terminated abruptly")
        future: concurrent.futures.Future[str] = concurrent.futures.Future()
        if self.mode == "ok":
            future.set_result("done")
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shut_down = True

    @property
    def killed(self) -> bool:
        return self._processes[1].terminated


def _done() -> str:
    return "done"


async def test_a_figure_call_out_of_time_kills_only_the_figure_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-review M2: the figure step's timeout never touches another
    # document's parse.
    main, figures = _Executor("ok"), _Executor("never")
    monkeypatch.setattr(ingestion, "_parse_executor", main)
    monkeypatch.setattr(FIGURE_POOL, "_executor", figures)
    with pytest.raises(DocumentParseError, match="out of time"):
        await FIGURE_POOL.run("a.pdf", _done, budget=PoolBudget(0.05))
    assert figures.killed and figures.shut_down
    assert not main.killed and not main.shut_down
    assert ingestion._parse_executor is main
    assert FIGURE_POOL._executor is None  # rebuilt on the next call


async def test_a_parse_broken_by_a_siblings_timeout_keeps_its_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-review M2: a pool killed for another document's timeout is not this
    # document's failure — after it, the document still has both attempts
    # (here: one more genuinely broken pool, then a working one).
    killed = _Executor("broken")
    ingestion._killed_pools.add(killed)
    following = iter([_Executor("broken"), _Executor("ok")])
    monkeypatch.setattr(ingestion, "_parse_executor", killed)
    monkeypatch.setattr(ingestion, "ProcessPoolExecutor", lambda **_: next(following))
    assert await ingestion._in_parse_pool("a.pdf", _done) == "done"


async def test_a_genuinely_broken_pool_still_gets_one_retry_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    following = iter([_Executor("broken"), _Executor("ok")])
    monkeypatch.setattr(ingestion, "_parse_executor", _Executor("broken"))
    monkeypatch.setattr(ingestion, "ProcessPoolExecutor", lambda **_: next(following))
    with pytest.raises(BrokenProcessPool):
        await ingestion._in_parse_pool("a.pdf", _done)


async def test_a_parse_timeout_marks_its_pool_as_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    wedged = _Executor("never")
    monkeypatch.setattr(ingestion, "_parse_executor", wedged)
    monkeypatch.setattr(ingestion, "_parse_timeout_s", 0.05)
    with pytest.raises(DocumentParseError, match="timed out"):
        await ingestion._in_parse_pool("a.pdf", _done)
    assert wedged in ingestion._killed_pools and wedged.killed
    await asyncio.sleep(0)
