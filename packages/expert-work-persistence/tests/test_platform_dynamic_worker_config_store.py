import pytest

from expert_work.persistence.platform_dynamic_worker_config import (
    InMemoryPlatformDynamicWorkerConfigStore,
)


@pytest.mark.asyncio
async def test_get_returns_none_when_unset() -> None:
    store = InMemoryPlatformDynamicWorkerConfigStore()
    assert await store.get() is None


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    store = InMemoryPlatformDynamicWorkerConfigStore()
    await store.put(
        max_concurrent=5,
        max_per_run=32,
        max_iterations=48,
        cap_max_concurrent=10,
        cap_max_per_run=64,
        cap_max_iterations=128,
        updated_by="admin-1",
    )
    row = await store.get()
    assert row is not None
    assert (row.max_concurrent, row.max_per_run, row.max_iterations) == (5, 32, 48)
    assert (row.cap_max_concurrent, row.cap_max_per_run, row.cap_max_iterations) == (10, 64, 128)
    assert row.updated_by == "admin-1"


@pytest.mark.asyncio
async def test_put_is_last_write_wins_singleton() -> None:
    store = InMemoryPlatformDynamicWorkerConfigStore()
    await store.put(
        max_concurrent=2,
        max_per_run=8,
        max_iterations=16,
        cap_max_concurrent=4,
        cap_max_per_run=16,
        cap_max_iterations=32,
        updated_by="a",
    )
    await store.put(
        max_concurrent=4,
        max_per_run=64,
        max_iterations=32,
        cap_max_concurrent=8,
        cap_max_per_run=128,
        cap_max_iterations=64,
        updated_by="b",
    )
    row = await store.get()
    assert row is not None
    assert (row.max_concurrent, row.max_per_run, row.max_iterations) == (4, 64, 32)
    assert (row.cap_max_concurrent, row.cap_max_per_run, row.cap_max_iterations) == (8, 128, 64)
    assert row.updated_by == "b"
