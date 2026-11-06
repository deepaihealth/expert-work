"""B-285 —— 轨迹对象的留存规则(:mod:`retention_cleanup_job.trajectories`)。"""

from __future__ import annotations

import logging
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
