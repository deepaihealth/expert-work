"""B-222 —— a pool call whose ingest is cancelled on this pod.

The asyncio side cannot stop a call already running in a pool worker. Its
slot is handed on only when the worker is free (or the next document's call
would wait in the pool's queue on its own clock), and the call keeps its
deadline: past it the pool is killed — the only way to free a worker — and the
slot is handed on. Parse pool and figure pool alike.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from control_plane.knowledge import ingestion
from control_plane.knowledge.parse_workers import FigurePool, PoolBudget


@pytest.fixture(autouse=True)
def _one_parse_slot(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ingestion, "_parse_workers", 1)
    monkeypatch.setattr(ingestion, "_parse_slots", None)
    yield


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _run_until_released(started: threading.Event, release: threading.Event) -> str:
    started.set()
    release.wait(10)
    return "parsed"


async def _cancel_while_running(call: asyncio.Task[str], started: threading.Event) -> None:
    assert await asyncio.to_thread(started.wait, 10)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)


async def test_a_cancelled_parse_keeps_its_slot_until_the_worker_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ingestion, "_parse_executor", pool)
    started, release = threading.Event(), threading.Event()
    try:
        parse = asyncio.create_task(
            ingestion._in_parse_pool("a.pdf", _run_until_released, started, release)
        )
        await _cancel_while_running(parse, started)
        slots = ingestion._get_parse_slots()

        assert slots.locked()  # the worker is still parsing, well within its time
        release.set()
        await _wait_until(lambda: not slots.locked())
        assert ingestion._parse_executor is pool  # done in time: the pool is kept
    finally:
        release.set()
        pool.shutdown(wait=True)


async def test_a_cancelled_parse_still_ends_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ingestion, "_parse_executor", pool)
    monkeypatch.setattr(ingestion, "_parse_timeout_s", 0.5)
    started, release = threading.Event(), threading.Event()
    try:
        parse = asyncio.create_task(
            ingestion._in_parse_pool("a.pdf", _run_until_released, started, release)
        )
        await _cancel_while_running(parse, started)
        slots = ingestion._get_parse_slots()
        assert slots.locked()

        # Past its 0.5 s the pool is killed and the slot handed on, though the
        # worker (a thread here, that no kill reaches) is still running.
        await _wait_until(lambda: not slots.locked(), timeout=5)
        assert not release.is_set()
        assert pool in ingestion._killed_pools  # its other calls are spared a retry
        assert ingestion._parse_executor is not pool  # the next parse gets a new pool

        # The worker finishing later hands nothing back a second time.
        release.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        await asyncio.sleep(0.05)  # the call's done-callback runs on the loop
        assert slots._value == 1
    finally:
        release.set()
        pool.shutdown(wait=True)


async def test_a_cancelled_parse_in_the_process_pool_is_killed_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real (spawned) parse pool: the overdue worker is terminated."""
    monkeypatch.setattr(ingestion, "_parse_executor", None)
    monkeypatch.setattr(ingestion, "_parse_timeout_s", 2.0)
    try:
        parse = asyncio.create_task(ingestion._in_parse_pool("wedged.pdf", time.sleep, 60))
        await _wait_until(
            lambda: (
                ingestion._parse_executor is not None
                and bool(getattr(ingestion._parse_executor, "_processes", None))
            ),
            timeout=30,
        )
        executor = ingestion._parse_executor
        workers = list(getattr(executor, "_processes", {}).values())
        parse.cancel()
        await asyncio.gather(parse, return_exceptions=True)
        slots = ingestion._get_parse_slots()

        await _wait_until(lambda: not slots.locked(), timeout=30)
        await _wait_until(lambda: not any(worker.is_alive() for worker in workers), timeout=30)
        assert ingestion._parse_executor is not executor
    finally:
        ingestion.shutdown_parse_pool(kill=True)


async def test_a_cancelled_figure_call_keeps_its_slot_until_its_deadline() -> None:
    figures = FigurePool()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    figures._executor = pool
    started, release = threading.Event(), threading.Event()
    try:
        call = asyncio.create_task(
            figures.run("a.pdf", _run_until_released, started, release, budget=PoolBudget(0.5))
        )
        await _cancel_while_running(call, started)
        slots = figures._get_slots()
        assert slots.locked()

        await _wait_until(lambda: not slots.locked(), timeout=5)
        assert not release.is_set()
        assert figures._executor is not pool  # killed at the deadline
    finally:
        release.set()
        pool.shutdown(wait=True)
        figures.shutdown(kill=True)
