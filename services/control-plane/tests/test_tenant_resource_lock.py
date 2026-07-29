"""Unit tests for :func:`control_plane._tenant_resource_lock.tenant_resource_lock`.

Uses :class:`FakeAdvisoryLockSessionFactory` (no real DB) to drive the
helper's own contract — retry-once-then-429, no-op on ``session_factory
=None``, per-key exclusion, and release-on-exception. Genuine
cross-connection mutual exclusion against a real Postgres is proven
separately in ``test_tenant_resource_lock_integration.py``.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

from control_plane._tenant_resource_lock import tenant_resource_lock
from tests.fake_advisory_lock import FakeAdvisoryLockSessionFactory


@pytest.mark.asyncio
async def test_none_session_factory_is_noop_both_proceed() -> None:
    """Single-process / in-memory stack — no cross-replica race, no lock."""
    tenant_id = uuid4()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with tenant_resource_lock(None, tenant_id, "eval_dataset"):
            order.append(f"{name}-enter")
            await asyncio.sleep(0.05)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A"), worker("B"))

    # No exclusion at all — both enter before either exits.
    assert order[0].endswith("-enter")
    assert order[1].endswith("-enter")


@pytest.mark.asyncio
async def test_contended_key_second_racer_gets_429_with_resource_kind_in_detail() -> None:
    """First holder outlives the retry budget (1 try + 1 retry @ 50ms) —
    the second racer must fail fast with 429, not queue."""
    factory = FakeAdvisoryLockSessionFactory()
    tenant_id = uuid4()

    async def holder() -> None:
        async with tenant_resource_lock(factory, tenant_id, "eval_dataset"):
            await asyncio.sleep(0.3)  # well past the retry window

    async def racer() -> HTTPException:
        await asyncio.sleep(0.01)  # let ``holder`` acquire first
        with pytest.raises(HTTPException) as exc_info:
            async with tenant_resource_lock(factory, tenant_id, "eval_dataset"):
                pass  # pragma: no cover - never reached
        return exc_info.value

    _, exc = await asyncio.gather(holder(), racer())
    assert exc.status_code == 429
    assert "eval_dataset" in str(exc.detail)


@pytest.mark.asyncio
async def test_second_racer_succeeds_if_first_releases_within_retry_window() -> None:
    """First holder releases quickly — the second racer's single retry
    finds the lock free and proceeds normally (no exception)."""
    factory = FakeAdvisoryLockSessionFactory()
    tenant_id = uuid4()
    order: list[str] = []

    async def holder() -> None:
        async with tenant_resource_lock(factory, tenant_id, "cron_trigger"):
            order.append("holder-enter")
            await asyncio.sleep(0.01)  # released well before the 50ms retry fires
            order.append("holder-exit")

    async def racer() -> None:
        await asyncio.sleep(0.005)  # start just after the holder acquires
        async with tenant_resource_lock(factory, tenant_id, "cron_trigger"):
            order.append("racer-enter")

    await asyncio.gather(holder(), racer())

    assert order[0] == "holder-enter"
    assert order[1] == "holder-exit"
    assert order[2] == "racer-enter"


@pytest.mark.asyncio
async def test_different_keys_do_not_contend() -> None:
    """Distinct ``(tenant_id, resource_kind)`` pairs run fully concurrently."""
    factory = FakeAdvisoryLockSessionFactory()
    order: list[str] = []

    async def worker(name: str, tenant_id: object, resource_kind: str) -> None:
        async with tenant_resource_lock(factory, tenant_id, resource_kind):  # type: ignore[arg-type]
            order.append(f"{name}-enter")
            await asyncio.sleep(0.05)
            order.append(f"{name}-exit")

    await asyncio.gather(
        worker("A", uuid4(), "eval_dataset"),
        worker("B", uuid4(), "eval_dataset"),  # different tenant, same resource_kind
    )
    assert order[0].endswith("-enter")
    assert order[1].endswith("-enter")


@pytest.mark.asyncio
async def test_lock_released_on_exception_inside_critical_section() -> None:
    """A caller-raised exception inside the ``async with`` still releases
    the lock — the next acquire on the same key must succeed."""
    factory = FakeAdvisoryLockSessionFactory()
    tenant_id = uuid4()

    with pytest.raises(ValueError, match="boom"):
        async with tenant_resource_lock(factory, tenant_id, "webhook_endpoint"):
            msg = "boom"
            raise ValueError(msg)

    # Lock was released — a fresh acquire on the same key doesn't 429.
    async with tenant_resource_lock(factory, tenant_id, "webhook_endpoint"):
        pass
