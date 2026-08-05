"""Tests for :class:`SandboxEgressMetricsWorker` — sandbox-egress-age-quota Task 3."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from control_plane.sandbox_egress_metrics import (
    _BLOCKED,
    _CYCLE_ERRORS,
    SandboxEgressMetricsWorker,
)
from expert_work.persistence.sandbox_egress_audit import (
    EgressAuditRecord,
    InMemorySandboxEgressAuditStore,
)


def _rec(verdict: str, occurred_at: datetime) -> EgressAuditRecord:
    return EgressAuditRecord(
        id=1,
        tenant_id=None,
        agent_name="alpha",
        agent_version="1.0.0",
        sandbox_id="sbx-1",
        target_host="api.example.com",
        target_port=443,
        verdict=verdict,
        bytes_up=0,
        bytes_down=0,
        duration_ms=None,
        error_msg=None,
        occurred_at=occurred_at,
    )


def _blocked_value(verdict: str) -> float:
    return _BLOCKED.labels(verdict=verdict)._value.get()  # type: ignore[attr-defined,no-any-return]


def _cycle_errors_value() -> float:
    return _CYCLE_ERRORS._value.get()  # type: ignore[attr-defined,no-any-return]


@pytest.mark.asyncio
async def test_refresh_once_increments_counter_by_verdict() -> None:
    store = InMemorySandboxEgressAuditStore()
    worker = SandboxEgressMetricsWorker(audit_store=store)
    before_auth = _blocked_value("blocked_auth")
    before_ssrf = _blocked_value("blocked_ssrf")

    # Records land after the worker's cursor is seeded (construction time),
    # same ordering the real lifespan wiring guarantees.
    store.records = [
        _rec("blocked_auth", datetime.now(UTC)),
        _rec("blocked_auth", datetime.now(UTC)),
        _rec("blocked_ssrf", datetime.now(UTC)),
    ]

    assert await worker.refresh_once()

    assert _blocked_value("blocked_auth") == before_auth + 2
    assert _blocked_value("blocked_ssrf") == before_ssrf + 1


@pytest.mark.asyncio
async def test_refresh_once_failed_read_increments_cycle_errors_without_raising() -> None:
    # approval_metrics test precedent — subclass the real in-memory store
    # and override just the one method to fail.
    class _ExplodingStore(InMemorySandboxEgressAuditStore):
        async def count_by_verdict_since(
            self, *, since: datetime, until: datetime
        ) -> dict[str, int]:
            msg = "db away"
            raise RuntimeError(msg)

    before = _cycle_errors_value()

    worker = SandboxEgressMetricsWorker(audit_store=_ExplodingStore())
    assert not await worker.refresh_once()  # logged + counted, no raise

    assert _cycle_errors_value() == before + 1


@pytest.mark.asyncio
async def test_two_cycles_advance_cursor_with_no_gap_or_overlap() -> None:
    class _RecordingStore(InMemorySandboxEgressAuditStore):
        def __init__(self) -> None:
            super().__init__()
            self.since_calls: list[datetime] = []
            self.until_calls: list[datetime] = []

        async def count_by_verdict_since(
            self, *, since: datetime, until: datetime
        ) -> dict[str, int]:
            self.since_calls.append(since)
            self.until_calls.append(until)
            return await super().count_by_verdict_since(since=since, until=until)

    store = _RecordingStore()
    worker = SandboxEgressMetricsWorker(audit_store=store)

    assert await worker.refresh_once()
    assert await worker.refresh_once()

    # Second cycle's ``since`` is exactly the first cycle's ``until`` (the
    # scan-start time it captured as "now") — a row can land in only one of
    # the two consecutive [since, until) windows (fix round 1, double-count
    # guard).
    assert store.since_calls[1] == store.until_calls[0]


@pytest.mark.asyncio
async def test_start_refreshes_immediately_and_stop_joins() -> None:
    store = InMemorySandboxEgressAuditStore()
    worker = SandboxEgressMetricsWorker(audit_store=store, interval_s=3600)
    before = _blocked_value("blocked_auth")
    store.records = [_rec("blocked_auth", datetime.now(UTC))]

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
