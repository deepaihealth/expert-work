"""Tests for :class:`SandboxEgressMetricsWorker` — sandbox-egress-age-quota Task 3."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from control_plane.sandbox_egress_metrics import (
    _BLOCKED,
    _CYCLE_ERRORS,
    SandboxEgressMetricsWorker,
)


@dataclass
class _FakeAuditStore:
    """Records each ``since`` arg; returns queued results in order.

    Raises for call indices listed in ``raise_on``.
    """

    results: list[dict[str, int]] = field(default_factory=list)
    raise_on: frozenset[int] = frozenset()
    since_calls: list[datetime] = field(default_factory=list, init=False)

    async def count_by_verdict_since(self, *, since: datetime) -> dict[str, int]:
        idx = len(self.since_calls)
        self.since_calls.append(since)
        if idx in self.raise_on:
            msg = "db away"
            raise RuntimeError(msg)
        return self.results[idx] if idx < len(self.results) else {}


def _blocked_value(verdict: str) -> float:
    return _BLOCKED.labels(verdict=verdict)._value.get()  # type: ignore[attr-defined,no-any-return]


def _cycle_errors_value() -> float:
    return _CYCLE_ERRORS._value.get()  # type: ignore[attr-defined,no-any-return]


@pytest.mark.asyncio
async def test_refresh_once_increments_counter_by_verdict() -> None:
    store = _FakeAuditStore(results=[{"blocked_auth": 2, "blocked_ssrf": 1}])
    before_auth = _blocked_value("blocked_auth")
    before_ssrf = _blocked_value("blocked_ssrf")

    worker = SandboxEgressMetricsWorker(audit_store=store)
    assert await worker.refresh_once()

    assert _blocked_value("blocked_auth") == before_auth + 2
    assert _blocked_value("blocked_ssrf") == before_ssrf + 1


@pytest.mark.asyncio
async def test_refresh_once_failed_read_increments_cycle_errors_without_raising() -> None:
    store = _FakeAuditStore(raise_on=frozenset({0}))
    before = _cycle_errors_value()

    worker = SandboxEgressMetricsWorker(audit_store=store)
    assert not await worker.refresh_once()  # logged + counted, no raise

    assert _cycle_errors_value() == before + 1


@pytest.mark.asyncio
async def test_two_cycles_advance_cursor_to_prior_scan_start_time() -> None:
    store = _FakeAuditStore(results=[{}, {}])
    worker = SandboxEgressMetricsWorker(audit_store=store)

    before_first = datetime.now(UTC)
    assert await worker.refresh_once()
    after_first = datetime.now(UTC)

    assert await worker.refresh_once()

    # Second cycle's ``since`` is the timestamp the first cycle captured as
    # "now" — bracketed by wall-clock reads taken around that first call.
    assert before_first <= store.since_calls[1] <= after_first


@pytest.mark.asyncio
async def test_start_refreshes_immediately_and_stop_joins() -> None:
    store = _FakeAuditStore(results=[{"blocked_auth": 1}])
    before = _blocked_value("blocked_auth")
    worker = SandboxEgressMetricsWorker(audit_store=store, interval_s=3600)
    worker.start()
    try:
        # start() refreshes once before the first interval elapses.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert _blocked_value("blocked_auth") == before + 1
        assert worker.is_running
    finally:
        await worker.stop()
    assert not worker.is_running
