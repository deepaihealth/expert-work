"""Tests for :mod:`control_plane.credential_value_cache`."""

from __future__ import annotations

from uuid import uuid4

from control_plane.credential_value_cache import CredentialValueCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_get_miss_returns_none() -> None:
    cache = CredentialValueCache(clock=FakeClock())
    assert cache.get(uuid4(), "ref-a") is None
    assert len(cache) == 0


def test_put_then_get_hit() -> None:
    cache = CredentialValueCache(clock=FakeClock())
    tenant = uuid4()
    cache.put(tenant, "ref-a", "s3cr3t")
    assert cache.get(tenant, "ref-a") == "s3cr3t"
    assert len(cache) == 1


def test_ttl_expiry_misses_after_deadline() -> None:
    clock = FakeClock()
    cache = CredentialValueCache(ttl_s=300.0, clock=clock)
    tenant = uuid4()
    cache.put(tenant, "ref-a", "s3cr3t")
    clock.now = 299.9
    assert cache.get(tenant, "ref-a") == "s3cr3t"
    clock.now = 300.0
    assert cache.get(tenant, "ref-a") is None
    assert len(cache) == 0


def test_lru_evicts_oldest_when_full() -> None:
    cache = CredentialValueCache(max_size=2, clock=FakeClock())
    tenant = uuid4()
    cache.put(tenant, "ref-1", "v1")
    cache.put(tenant, "ref-2", "v2")
    cache.put(tenant, "ref-3", "v3")
    assert cache.get(tenant, "ref-1") is None
    assert cache.get(tenant, "ref-2") == "v2"
    assert cache.get(tenant, "ref-3") == "v3"
    assert len(cache) == 2


def test_get_refreshes_recency_so_eviction_targets_lru() -> None:
    cache = CredentialValueCache(max_size=2, clock=FakeClock())
    tenant = uuid4()
    cache.put(tenant, "ref-1", "v1")
    cache.put(tenant, "ref-2", "v2")
    assert cache.get(tenant, "ref-1") == "v1"
    cache.put(tenant, "ref-3", "v3")
    assert cache.get(tenant, "ref-2") is None
    assert cache.get(tenant, "ref-1") == "v1"


def test_invalidate_all_clears_everything() -> None:
    cache = CredentialValueCache(clock=FakeClock())
    cache.put(uuid4(), "ref-a", "v1")
    cache.put(uuid4(), "ref-b", "v2")
    cache.invalidate_all()
    assert len(cache) == 0


def test_invalidate_tenant_only_clears_that_tenant() -> None:
    cache = CredentialValueCache(clock=FakeClock())
    tenant_a = uuid4()
    tenant_b = uuid4()
    cache.put(tenant_a, "ref-1", "a1")
    cache.put(tenant_a, "ref-2", "a2")
    cache.put(tenant_b, "ref-1", "b1")
    cache.invalidate_tenant(tenant_a)
    assert cache.get(tenant_a, "ref-1") is None
    assert cache.get(tenant_a, "ref-2") is None
    assert cache.get(tenant_b, "ref-1") == "b1"
    assert len(cache) == 1
