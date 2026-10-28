"""解析进程池的公共部件:进程内存上限、杀进程、一份文档的时间预算,以及图片单独用的小进程池。

* :func:`limit_worker_memory` —— 进程池 worker 的初始化函数(安全复核 S-M1):给 worker 设地址空间
  上限(``RLIMIT_AS``),一份要太多内存的文档自己解析失败(``MemoryError``),而不是整个 pod 被
  内核杀掉。只在 Linux 上设(macOS 不执行);``0`` = 不设;不会调高已有的软 / 硬上限。
* :class:`PoolBudget` —— 一份文档在进程池里的时间预算;子预算花的时间也记在父预算上。
* :class:`FigurePool`(二轮复核 M2)—— 插图收集 / 扫描页裁图跑在自己的小进程池里:一次图片调用
  用完时间要杀进程时,只杀这个池(和池里的图片调用),不碰别的文档的解析。图片调用失败只让那份
  文档没有插图(调用方计为读不了),所以这里不重试。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import multiprocessing
import sys
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Any

from control_plane.knowledge.parsing import DocumentParseError

logger = logging.getLogger(__name__)

#: Figure-pool workers: figure collection is a small share of a document's
#: parse, and a lone worker means a killed pool never takes a sibling's
#: figure call with it (calls are admitted one per worker).
FIGURE_POOL_WORKERS = 1


def _on_linux() -> bool:
    return sys.platform.startswith("linux")


def limit_worker_memory(max_bytes: int) -> None:
    """Pool-worker initializer: cap the worker's address space (see module
    doc). Lowers the soft limit only to ``max_bytes`` or the existing soft
    limit, whichever is smaller; the hard limit is never raised."""
    if max_bytes <= 0 or not _on_linux():
        return
    try:
        import resource
    except ImportError:  # pragma: no cover - not POSIX
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = max_bytes
    for existing in (soft, hard):
        if existing != resource.RLIM_INFINITY:
            cap = min(cap, existing)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except (ValueError, OSError):
        logger.warning("knowledge.parse_worker_memory_limit_failed", exc_info=True)


def terminate_workers(executor: Executor) -> None:
    """Kill ``executor``'s worker processes — the only way to free a worker
    stuck in a call that timed out. ``ProcessPoolExecutor`` has no public
    kill; its ``_processes`` map is the stable-enough way to reach the
    workers (guarded: a thread pool or a test double has none)."""
    processes = getattr(executor, "_processes", None) or {}
    for process in list(processes.values()):
        try:
            process.terminate()
        except Exception:  # pragma: no cover - best effort
            logger.warning("knowledge.parse_pool_terminate_failed", exc_info=True)


class PoolSlot:
    """One call's hold on a pool's concurrency slot (B-222). Cancelled while
    its call still runs in a worker — the asyncio side cannot stop it (an
    ingest cancelled on this pod) — the slot is handed on only once that
    worker is done, or the next call would wait in the pool's queue on its
    own clock. The call keeps its deadline: past it ``kill`` frees the worker
    (the pool is killed) and the slot is handed on."""

    def __init__(self, slots: asyncio.Semaphore) -> None:
        self._slots = slots
        self._call: Future[Any] | None = None
        self._deadline = 0.0
        self._kill: Callable[[], None] = _no_kill
        self._held = False
        self._timer: asyncio.TimerHandle | None = None

    def holds(self, call: Future[Any], *, deadline: float, kill: Callable[[], None]) -> None:
        """The call now running on the slot, its deadline (loop time) and how
        to free its worker once it is overdue."""
        self._call, self._deadline, self._kill = call, deadline, kill

    async def __aenter__(self) -> PoolSlot:
        await self._slots.acquire()
        self._held = True
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        call = self._call
        cancelled = exc_type is not None and issubclass(exc_type, asyncio.CancelledError)
        if not cancelled or call is None or call.done():
            self._release()
            return
        loop = asyncio.get_running_loop()
        self._timer = loop.call_later(max(0.0, self._deadline - loop.time()), self._overdue)
        call.add_done_callback(lambda _: _soon(loop, self._release))

    def _overdue(self) -> None:
        if self._call is not None and not self._call.done():
            self._kill()
        self._release()

    def _release(self) -> None:
        """Hand the slot on — once (the call ending and its deadline both lead here)."""
        if self._timer is not None:
            self._timer.cancel()
        if self._held:
            self._held = False
            self._slots.release()


def _no_kill() -> None:
    return None


def _soon(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> None:
    """Run ``callback`` on ``loop`` from a pool's callback thread."""
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:  # pragma: no cover - the loop is gone (shutdown)
        pass


@dataclass
class PoolBudget:
    """B-209 (review M2) —— one parse-pool time budget per document: page
    analysis plus every render batch share the parse timeout, as the single
    ``parse_document`` call did before. Time queued for a slot is not charged.
    A sub-budget (``parent`` set) also spends its parent's."""

    remaining: float
    parent: PoolBudget | None = None

    def charge(self, seconds: float) -> None:
        self.remaining -= seconds
        if self.parent is not None:
            self.parent.charge(seconds)


class FigurePool:
    """The figure step's own process pool (see module doc)."""

    def __init__(self) -> None:
        self._executor: Executor | None = None
        self._slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
        self._workers = FIGURE_POOL_WORKERS
        self._max_vm_bytes = 0

    def configure(self, *, workers: int, max_vm_bytes: int) -> None:
        """Size and per-worker memory ceiling; for the next pool created."""
        self._workers = max(1, workers)
        self._max_vm_bytes = max(0, max_vm_bytes)
        self._slots = None

    async def run[T](
        self, filename: str, fn: Callable[..., T], *args: object, budget: PoolBudget
    ) -> T:
        """``fn(*args)`` in this pool within ``budget``; a timeout kills this
        pool only. A pool found broken when the call is handed over (its
        worker died earlier) is rebuilt and the call handed to the new one
        once. Raises :class:`DocumentParseError` (out of time / memory) or
        ``BrokenProcessPool`` — the caller falls back to "no figures"."""
        async with PoolSlot(self._get_slots()) as slot:
            if budget.remaining <= 0:
                msg = f"figures of {filename!r} out of time"
                raise DocumentParseError(msg)
            loop = asyncio.get_running_loop()
            executor, call = self._submit(fn, args)
            started = loop.time()
            slot.holds(
                call,
                deadline=started + budget.remaining,
                kill=functools.partial(self._kill_overdue, executor, filename),
            )
            try:
                return await asyncio.wait_for(asyncio.wrap_future(call), timeout=budget.remaining)
            except TimeoutError as exc:
                logger.warning("knowledge.figure_pool_timeout document=%r — killing it", filename)
                self._discard(executor)
                msg = f"figures of {filename!r} out of time"
                raise DocumentParseError(msg) from exc
            except BrokenProcessPool:
                self._discard(executor)
                raise
            except MemoryError as exc:
                msg = f"figures of {filename!r} need more memory than a parse may use"
                raise DocumentParseError(msg) from exc
            finally:
                budget.charge(loop.time() - started)

    def _kill_overdue(self, executor: Executor, filename: str) -> None:
        """A cancelled figure call past its time (B-222): free its worker."""
        logger.warning("knowledge.figure_pool_overdue document=%r — killing it", filename)
        self._discard(executor)

    def _submit(
        self, fn: Callable[..., object], args: tuple[object, ...]
    ) -> tuple[Executor, Future[Any]]:
        for attempt in (1, 2):
            executor = self._get_executor()
            try:
                return executor, executor.submit(fn, *args)
            except BrokenProcessPool:
                self._discard(executor)
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def shutdown(self, *, kill: bool = False) -> None:
        executor, self._executor = self._executor, None
        if executor is None:
            return
        if kill:
            terminate_workers(executor)
        executor.shutdown(wait=False, cancel_futures=True)

    def _discard(self, executor: Executor) -> None:
        if self._executor is executor:
            self.shutdown(kill=True)

    def _get_executor(self) -> Executor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self._workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=limit_worker_memory,
                initargs=(self._max_vm_bytes,),
            )
        return self._executor

    def _get_slots(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots[0] is not loop:
            self._slots = (loop, asyncio.Semaphore(self._workers))
        return self._slots[1]


#: The process-wide figure pool (configured with the parse pool).
FIGURE_POOL = FigurePool()
