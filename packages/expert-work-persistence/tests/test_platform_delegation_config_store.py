import pytest

from expert_work.persistence.platform_delegation_config import (
    InMemoryPlatformDelegationConfigStore,
)


@pytest.mark.asyncio
async def test_get_returns_none_when_unset() -> None:
    store = InMemoryPlatformDelegationConfigStore()
    assert await store.get() is None


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    store = InMemoryPlatformDelegationConfigStore()
    await store.put(max_concurrent_delegations=5, updated_by="admin-1")
    row = await store.get()
    assert row is not None
    assert row.max_concurrent_delegations == 5
    assert row.updated_by == "admin-1"


@pytest.mark.asyncio
async def test_put_is_last_write_wins_singleton() -> None:
    store = InMemoryPlatformDelegationConfigStore()
    await store.put(max_concurrent_delegations=2, updated_by="a")
    await store.put(max_concurrent_delegations=4, updated_by="b")
    row = await store.get()
    assert row is not None
    assert row.max_concurrent_delegations == 4
    assert row.updated_by == "b"
