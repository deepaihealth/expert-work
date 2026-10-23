"""B-209 PR-2 —— parse-pool limits: a worker's memory ceiling (security review
S-M1), the figure step's share of a document's time budget (review H3), and
the figure step's own pool (re-review M2)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
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
    ingestion.configure_parse_pool(workers=2, timeout_s=60, max_vm_mb=2400, figure_max_vm_mb=1536)
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
    # Round 5 (B): the figure worker has its own, lower ceiling.
    assert (main["initargs"], figures["initargs"]) == ((2400 << 20,), (1536 << 20,))
    assert (main["max_workers"], figures["max_workers"]) == (2, 1)


async def test_the_lifespan_hands_both_ceilings_to_the_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane import app as app_module
    from control_plane.settings import Settings
    from tests.auth_fixtures import build_test_jwt_verifier

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(app_module, "configure_parse_pool", lambda **kwargs: seen.append(kwargs))
    settings = Settings(
        checkpointer_backend="memory",
        knowledge_parse_worker_max_vm_mb=2222,
        knowledge_figure_worker_max_vm_mb=1111,
    )
    app = app_module.create_app(
        settings=settings,
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        pass
    assert [(k["max_vm_mb"], k["figure_max_vm_mb"]) for k in seen] == [(2222, 1111)]


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


class _Delayed(concurrent.futures.Executor):
    """A pool whose call ends ``after`` seconds in: broken (killed for another
    document's timeout) or done."""

    def __init__(self, after: float, *, broken: bool) -> None:
        self._after = after
        self._broken = broken
        self._processes: dict[int, Any] = {}

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        future: concurrent.futures.Future[str] = concurrent.futures.Future()
        future.set_running_or_notify_cancel()

        def finish() -> None:
            if self._broken:
                future.set_exception(BrokenProcessPool("killed"))
            else:
                future.set_result("done")

        threading.Timer(self._after, finish).start()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


async def test_a_spared_attempt_costs_the_document_no_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-review round 4 (M2, the reviewer's probe): killed 0.6 s into the call
    # for another document's timeout; the retry needs 0.5 s of a 1 s budget.
    killed = _Delayed(0.6, broken=True)
    ingestion._killed_pools.add(killed)
    monkeypatch.setattr(ingestion, "_parse_executor", killed)
    monkeypatch.setattr(ingestion, "ProcessPoolExecutor", lambda **_: _Delayed(0.5, broken=False))
    budget = PoolBudget(1.0)
    assert await ingestion._in_parse_pool("victim.docx", _done, budget=budget) == "done"
    assert 0.3 < budget.remaining < 0.55


async def test_sparing_stops_after_three_sibling_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three sibling kills are spared; from the fourth on the attempts count.
    pools = [_Executor("broken") for _ in range(5)]
    for pool in pools:
        ingestion._killed_pools.add(pool)
    following = iter(pools[1:])
    monkeypatch.setattr(ingestion, "_parse_executor", pools[0])
    monkeypatch.setattr(ingestion, "ProcessPoolExecutor", lambda **_: next(following))
    with pytest.raises(BrokenProcessPool):
        await ingestion._in_parse_pool("a.pdf", _done)


async def test_an_out_of_time_message_names_the_time_it_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingestion, "_parse_executor", _Executor("never"))
    monkeypatch.setattr(ingestion, "_parse_timeout_s", 600.0)
    with pytest.raises(DocumentParseError, match=r"after 0\.05s \(what was left of its 600s\)"):
        await ingestion._in_parse_pool("a.pdf", _done, budget=PoolBudget(0.05))
    with pytest.raises(DocumentParseError, match="its 600s are used up"):
        await ingestion._in_parse_pool("a.pdf", _done, budget=PoolBudget(0.0))


async def test_a_figure_pool_found_broken_is_rebuilt_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-review round 4 (L11): its worker died earlier — the next document's
    # figures still get a pool.
    broken = _Executor("broken")
    monkeypatch.setattr(FIGURE_POOL, "_executor", broken)
    monkeypatch.setattr(parse_workers, "ProcessPoolExecutor", lambda **_: _Executor("ok"))
    assert await FIGURE_POOL.run("a.pdf", _done, budget=PoolBudget(5.0)) == "done"
    assert broken.shut_down
    # Broken again right after the rebuild: the call gives up (no figures).
    monkeypatch.setattr(FIGURE_POOL, "_executor", _Executor("broken"))
    monkeypatch.setattr(parse_workers, "ProcessPoolExecutor", lambda **_: _Executor("broken"))
    with pytest.raises(BrokenProcessPool):
        await FIGURE_POOL.run("a.pdf", _done, budget=PoolBudget(5.0))
    monkeypatch.setattr(FIGURE_POOL, "_executor", None)
