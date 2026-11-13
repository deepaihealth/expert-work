"""Cooperative run cancellation — Stream E.15.

A :class:`CancellationToken` is the orchestrator's cooperative-cancel
primitive. "Cooperative" means cancellation surfaces only at explicit
checkpoints — node entries and the LLM / tool ``await`` boundaries —
never via a hard kill (Mini-ADR E-9: hard kill leaves half-written
checkpoints / half-sent SSE events; SIGKILL is the F.7 sandbox
supervisor's job, not the orchestrator's).

Two surfacing mechanisms:

- :meth:`raise_if_cancelled` — a synchronous checkpoint. Nodes call it
  on entry so a run cancelled between steps stops immediately.
- :meth:`run_cancellable` — wraps a single ``await`` (an LLM call, a
  tool dispatch). It races the awaited coroutine against the cancel
  signal; if cancel wins, the in-flight coroutine is cancelled and
  :class:`RunCancelledError` is raised — so a 30-second LLM call is
  interrupted within one event-loop tick of the cancel, not after it
  finishes.

The token is backed by an :class:`asyncio.Event`. Production wires it
to a run's ``RunRecord.abort_event`` via :meth:`from_event`, so
``RunManager.cancel`` and the token share one signal. The token is
threaded to graph nodes through ``config["configurable"]`` — **not**
through ``AgentState`` — because a live :class:`asyncio.Event` is not
checkpoint-serialisable, and every TypedDict state channel is
checkpointed.

Per [STREAM-E-DESIGN § 2.7](../../../../../../docs/streams/STREAM-E-DESIGN.md).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Self, TypeVar

T = TypeVar("T")

#: ``config["configurable"]`` key under which the active token travels.
#: noqa S105: this is a config dict key, not a credential.
CANCELLATION_TOKEN_KEY = "cancellation_token"  # noqa: S105


class RunCancelledError(Exception):
    """Raised at a cancellation checkpoint when the run has been cancelled.

    Distinct from :class:`asyncio.CancelledError`: this is a *normal*
    domain exception the orchestrator catches to finalise the run as
    ``INTERRUPTED`` + emit a ``run:cancelled`` audit — it must not be
    confused with task-level asyncio cancellation, and (unlike
    ``CancelledError``) it is safe to catch with a bare ``except``.
    """


@dataclass
class CancellationToken:
    """Cooperative cancellation signal shared across one run.

    Construct a fresh token with ``CancellationToken()`` (tests / dev),
    or :meth:`from_event` to bind it to an existing
    :class:`asyncio.Event` such as ``RunRecord.abort_event``.
    """

    _event: asyncio.Event = field(default_factory=asyncio.Event)

    @classmethod
    def from_event(cls, event: asyncio.Event) -> Self:
        """Wrap an existing event so the token and its owner share a signal."""
        return cls(_event=event)

    def cancel(self) -> None:
        """Signal cancellation. Idempotent."""
        self._event.set()

    def cancelled(self) -> bool:
        """Return whether cancellation has been signalled."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`RunCancelledError` if already cancelled — else no-op.

        The synchronous checkpoint. Call it at node entry so a run
        cancelled between steps aborts before doing more work.
        """
        if self._event.is_set():
            raise RunCancelledError("run cancelled")

    async def run_cancellable(self, coro: Coroutine[object, object, T]) -> T:
        """Await ``coro``, but abort it if cancellation fires first.

        Races ``coro`` against the cancel event. If ``coro`` finishes
        first, its result is returned. If cancellation wins, ``coro``'s
        task is cancelled (so the in-flight LLM / tool ``await`` is
        actually interrupted) and :class:`RunCancelledError` is raised.

        Already-cancelled tokens raise immediately without scheduling
        ``coro`` — but ``coro`` is still closed so the event loop does
        not warn about a never-awaited coroutine.
        """
        if self._event.is_set():
            coro.close()
            raise RunCancelledError("run cancelled")

        task: asyncio.Task[T] = asyncio.ensure_future(coro)
        waiter: asyncio.Task[bool] = asyncio.ensure_future(self._event.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # B-300 review L3 —— the caller itself was cancelled (e.g. a sibling in a
            # TaskGroup failed): interrupt the in-flight call too instead of leaving
            # it running with nobody awaiting it.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            waiter.cancel()
            # Drain the cancelled waiter. ``gather(return_exceptions=True)``
            # swallows its CancelledError and — being a call expression —
            # avoids CodeQL's py/ineffectual-statement on a bare await.
            await asyncio.gather(waiter, return_exceptions=True)

        if task.done():
            return task.result()

        # Cancellation won the race — interrupt the in-flight coroutine
        # and drain it (capturing whatever it raises on cancel).
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise RunCancelledError("run cancelled mid-call")
