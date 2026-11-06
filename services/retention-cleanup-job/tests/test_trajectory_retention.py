"""B-285 —— 轨迹对象的留存规则(:mod:`retention_cleanup_job.trajectories`)。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryThreadMetaStore
from expert_work.protocol.trajectory_keys import trajectory_key
from expert_work.runtime.storage import InMemoryObjectStore
from retention_cleanup_job.report import CleanupReport
from retention_cleanup_job.trajectories import sweep_trajectories

_NOW = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)


async def _thread(threads: InMemoryThreadMetaStore, tenant: UUID) -> UUID:
    thread = uuid4()
    await threads.create(
        thread_id=thread,
        tenant_id=tenant,
        created_by="t",
        user_id=uuid4(),
        agent_name="a",
        agent_version="1",
    )
    return thread


async def _put(
    store: InMemoryObjectStore,
    tenant: UUID,
    thread: UUID,
    *,
    days_ago: int,
    conversation: UUID | None = None,
) -> str:
    key = trajectory_key(
        tenant_id=tenant,
        outcome="success",
        at=_NOW - timedelta(days=days_ago),
        thread_id=thread,
        conversation_id=conversation,
    )
    await store.put(key, b"{}")
    return key


@pytest.mark.asyncio
async def test_expired_and_orphaned_objects_go_live_recent_ones_stay() -> None:
    store, threads = InMemoryObjectStore(), InMemoryThreadMetaStore()
    short, default = uuid4(), uuid4()
    live_short = await _thread(threads, short)
    live_default = await _thread(threads, default)
    gone = uuid4()

    expired_short = await _put(store, short, live_short, days_ago=31)  # tenant keeps 30 days
    kept_short = await _put(store, short, live_short, days_ago=29)
    kept_default = await _put(store, default, live_default, days_ago=60)  # default 90
    expired_default = await _put(store, default, live_default, days_ago=91)
    orphan = await _put(store, default, gone, days_ago=1)
    child_kept = await _put(store, default, uuid4(), days_ago=1, conversation=live_default)
    child_orphan = await _put(store, default, uuid4(), days_ago=1, conversation=gone)
    # Pre-B-285 sub-agent key: flat, its thread never had a row.
    legacy_child = await _put(store, default, uuid4(), days_ago=1)
    stranger = "trajectories/not-a-tenant/success/x.jsonl"
    await store.put(stranger, b"{}")

    tally = await sweep_trajectories(
        store=store,
        threads=threads,
        retention_days={short: 30, default: None},
        default_days=90,
        now=_NOW,
        limit=100,
    )

    assert (tally.expired_removed, tally.orphans_removed, tally.failed) == (2, 3, 0)
    assert tally.unrecognised == 1
    assert sorted(await store.list_prefix("trajectories/")) == sorted(
        [kept_short, kept_default, child_kept, stranger]
    )
    for key in (expired_short, expired_default, orphan, child_orphan, legacy_child):
        assert key not in await store.list_prefix("trajectories/")


@pytest.mark.asyncio
async def test_a_run_is_capped_and_the_rest_waits_for_tomorrow() -> None:
    store, threads = InMemoryObjectStore(), InMemoryThreadMetaStore()
    tenant = uuid4()
    for _ in range(5):
        await _put(store, tenant, uuid4(), days_ago=1)

    tally = await sweep_trajectories(
        store=store, threads=threads, retention_days={}, default_days=90, now=_NOW, limit=3
    )

    assert tally.orphans_removed == 3
    assert tally.capped
    assert len(await store.list_prefix("trajectories/")) == 2


class _DeleteFails(InMemoryObjectStore):
    async def delete(self, key: str) -> None:
        raise TimeoutError("oss")


@pytest.mark.asyncio
async def test_a_delete_failure_is_counted_and_needs_attention() -> None:
    store, threads = _DeleteFails(), InMemoryThreadMetaStore()
    key = await _put(store, uuid4(), uuid4(), days_ago=1)

    tally = await sweep_trajectories(
        store=store, threads=threads, retention_days={}, default_days=90, now=_NOW, limit=10
    )

    assert tally.failed == 1
    assert tally.needs_attention
    assert await store.list_prefix("trajectories/") == [key]


def test_the_done_line_carries_the_counters(caplog: pytest.LogCaptureFixture) -> None:
    from retention_cleanup_job.main import log_done

    with caplog.at_level(logging.INFO):
        log_done(
            CleanupReport(
                trajectories_expired=4, trajectory_orphans_removed=2, trajectories_failed=1
            )
        )
    line = next(
        r.getMessage() for r in caplog.records if "retention_cleanup_job.done" in r.getMessage()
    )
    assert "trajectories_expired=4" in line
    assert "trajectory_orphans_removed=2" in line
    assert "trajectories_failed=1" in line


class _Paged(InMemoryObjectStore):
    """Lists in pages of 2 and counts how many pages were handed out."""

    def __init__(self) -> None:
        super().__init__()
        self.pages = 0

    async def iter_prefix_modified(
        self, prefix: str, page_size: int = 2
    ) -> AsyncIterator[list[tuple[str, datetime]]]:
        listed = await self.list_prefix_modified(prefix)
        for start in range(0, len(listed), 2):
            self.pages += 1
            yield listed[start : start + 2]


@pytest.mark.asyncio
async def test_listing_is_paged_and_stops_once_the_budget_is_spent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, threads = _Paged(), InMemoryThreadMetaStore()
    tenant = uuid4()
    for _ in range(10):  # 10 orphans -> 5 pages
        await _put(store, tenant, uuid4(), days_ago=1)

    with caplog.at_level(logging.INFO):
        tally = await sweep_trajectories(
            store=store, threads=threads, retention_days={}, default_days=90, now=_NOW, limit=3
        )

    assert tally.orphans_removed == 3
    assert tally.capped
    assert any("retention.trajectories_capped" in r.getMessage() for r in caplog.records)
    assert store.pages == 2  # budget spent inside page 2: page 3 is never listed
    assert tally.scanned == 4


class _FirstDeletesFail(InMemoryObjectStore):
    def __init__(self, failing: int) -> None:
        super().__init__()
        self.failing = failing

    async def delete(self, key: str) -> None:
        if self.failing > 0:
            self.failing -= 1
            raise TimeoutError("oss")
        await super().delete(key)


@pytest.mark.asyncio
async def test_failed_deletes_do_not_use_up_the_budget() -> None:
    store, threads = _FirstDeletesFail(failing=2), InMemoryThreadMetaStore()
    tenant = uuid4()
    for _ in range(6):
        await _put(store, tenant, uuid4(), days_ago=1)

    tally = await sweep_trajectories(
        store=store, threads=threads, retention_days={}, default_days=90, now=_NOW, limit=3
    )

    assert (tally.failed, tally.orphans_removed) == (2, 3)
    assert tally.needs_attention
    assert not tally.aborted


@pytest.mark.asyncio
async def test_too_many_failures_stop_the_pass() -> None:
    store, threads = _DeleteFails(), InMemoryThreadMetaStore()
    tenant = uuid4()
    for _ in range(10):
        await _put(store, tenant, uuid4(), days_ago=1)

    tally = await sweep_trajectories(
        store=store,
        threads=threads,
        retention_days={},
        default_days=90,
        now=_NOW,
        limit=100,
        max_failures=3,
    )

    assert tally.failed == 3
    assert tally.aborted
    assert tally.needs_attention


@pytest.mark.asyncio
async def test_a_job_without_an_object_store_says_it_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from retention_cleanup_job.job import RetentionCleanupJob

    job = RetentionCleanupJob(
        db_session_factory=None,  # type: ignore[arg-type]  # never reached on this path
        object_store=None,
        thread_store=InMemoryThreadMetaStore(),
    )
    with caplog.at_level(logging.WARNING):
        tally = await job._sweep_trajectories()

    assert tally.skipped
    assert any(
        "retention.trajectories_skipped reason=no_object_store" in r.getMessage()
        for r in caplog.records
    )


def test_the_done_line_tells_skipped_and_capped_apart(caplog: pytest.LogCaptureFixture) -> None:
    from retention_cleanup_job.main import log_done

    with caplog.at_level(logging.INFO):
        log_done(CleanupReport(trajectories_scanned=7, trajectories_capped=True))
        log_done(CleanupReport(trajectories_skipped=True))
    lines = [
        r.getMessage() for r in caplog.records if "retention_cleanup_job.done" in r.getMessage()
    ]
    assert "trajectories_scanned=7 trajectories_capped=True trajectories_skipped=False" in lines[0]
    assert "trajectories_scanned=0 trajectories_capped=False trajectories_skipped=True" in lines[1]
