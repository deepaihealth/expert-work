"""Unit tests for :class:`InMemoryQuotaService` — Stream C.5."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_plane.quota import InMemoryQuotaService
from expert_work.persistence.quota import (
    BudgetExceededError,
    InMemoryTenantQuotaStore,
    InMemoryTokenReservationStore,
)
from expert_work.protocol import (
    CheckRequest,
    CommitRequest,
    QuotaDimension,
    ReservationState,
    ReserveRequest,
    TenantQuotaPatch,
)


def _tenant() -> UUID:
    return uuid4()


def _quota_store_with_qps(
    tenant_id: UUID, limit: int = 2, burst: int = 2, scope: dict[str, str] | None = None
) -> InMemoryTenantQuotaStore:
    """Build a store seeded with a single tenant QPS row."""
    store = InMemoryTenantQuotaStore()
    return store, TenantQuotaPatch(  # type: ignore[return-value]
        dimension=QuotaDimension.QPS,
        scope=scope or {},
        limit_value=limit,
        burst=burst,
    )


async def _seed(store: InMemoryTenantQuotaStore, tenant_id: UUID, patch: TenantQuotaPatch) -> None:
    await store.upsert(tenant_id=tenant_id, patch=patch, updated_by="test")


# ---------------------------------------------------------------------------
# check — single-tenant token bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_allows_within_burst() -> None:
    tenant = _tenant()
    quota_store, patch = _quota_store_with_qps(tenant, limit=10, burst=3)
    await _seed(quota_store, tenant, patch)
    svc = InMemoryQuotaService(
        quota_store=quota_store,
        reservation_store=InMemoryTokenReservationStore(),
    )

    for _ in range(3):
        result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
        assert result.allowed
    # 4th spend within the same millisecond exceeds burst.
    result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert not result.allowed
    assert result.blocked_dimension is QuotaDimension.QPS
    assert result.retry_after_s is not None and result.retry_after_s >= 0


@pytest.mark.asyncio
async def test_check_with_no_quota_is_unlimited() -> None:
    tenant = _tenant()
    svc = InMemoryQuotaService(
        quota_store=InMemoryTenantQuotaStore(),
        reservation_store=InMemoryTokenReservationStore(),
    )
    result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert result.allowed
    assert result.remaining == {}


@pytest.mark.asyncio
async def test_invalidate_tenant_makes_new_quota_rule_bite_immediately() -> None:
    # PR-E3b — the resolved quota rows are cached for 60s; a quota-admin write
    # must call ``invalidate_tenant`` so the new rule bites NOW, not a minute
    # later (before this PR neither pod dropped the cache).
    tenant = _tenant()
    quota_store = InMemoryTenantQuotaStore()
    svc = InMemoryQuotaService(
        quota_store=quota_store,
        reservation_store=InMemoryTokenReservationStore(),
    )
    # Cold check with no rules → unlimited, and the empty row set is cached.
    assert (await svc.check(CheckRequest(tenant_id=tenant, cost=1))).allowed
    # An admin now adds a burst-1 QPS rule …
    await _seed(
        quota_store,
        tenant,
        TenantQuotaPatch(dimension=QuotaDimension.QPS, scope={}, limit_value=1, burst=1),
    )
    # … which the cached (stale) empty view still ignores (no bucket at all) …
    stale = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert stale.allowed
    assert stale.remaining == {}
    # … until the cache is dropped: the rule loads, the single burst token is
    # spent, and the immediate follow-up check is denied.
    svc.invalidate_tenant(tenant)
    first = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert first.allowed
    assert "qps" in first.remaining
    assert not (await svc.check(CheckRequest(tenant_id=tenant, cost=1))).allowed


@pytest.mark.asyncio
async def test_check_uses_default_qps_when_configured() -> None:
    tenant = _tenant()
    svc = InMemoryQuotaService(
        quota_store=InMemoryTenantQuotaStore(),
        reservation_store=InMemoryTokenReservationStore(),
        default_qps_limit=5,
        default_qps_burst=2,
    )
    # 2 within burst → ok, 3rd → denied.
    assert (await svc.check(CheckRequest(tenant_id=tenant, cost=1))).allowed
    assert (await svc.check(CheckRequest(tenant_id=tenant, cost=1))).allowed
    assert not (await svc.check(CheckRequest(tenant_id=tenant, cost=1))).allowed


@pytest.mark.asyncio
async def test_check_scope_matches_agent() -> None:
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    # Agent-scoped row applies only when agent name matches.
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.QPS,
            scope={"agent": "alpha"},
            limit_value=10,
            burst=1,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    # Different agent → no dimension applies → unlimited.
    result_beta = await svc.check(CheckRequest(tenant_id=tenant, agent="beta", cost=1))
    assert result_beta.allowed
    assert result_beta.remaining == {}

    # alpha agent → burst=1, second spend denied.
    assert (await svc.check(CheckRequest(tenant_id=tenant, agent="alpha", cost=1))).allowed
    denied = await svc.check(CheckRequest(tenant_id=tenant, agent="alpha", cost=1))
    assert not denied.allowed


# ---------------------------------------------------------------------------
# reserve / commit / release — token reservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_grants_when_no_budget_configured() -> None:
    tenant = _tenant()
    svc = InMemoryQuotaService(
        quota_store=InMemoryTenantQuotaStore(),
        reservation_store=InMemoryTokenReservationStore(),
    )
    result = await svc.reserve_tokens(
        ReserveRequest(
            tenant_id=tenant,
            agent="alpha",
            thread_id=uuid4(),
            estimated_tokens=1000,
        )
    )
    assert result.granted
    assert result.reservation_id is not None
    assert result.reason == "ok"


@pytest.mark.asyncio
async def test_reserve_denies_when_over_monthly_budget() -> None:
    tenant = _tenant()
    reservations = InMemoryTokenReservationStore()
    # Configure a small budget so the reservation request overshoots.
    month = datetime.now(tz=UTC).date().replace(day=1)
    await reservations.set_budget_total_for_test(tenant_id=tenant, month=month, budget_total=500)
    svc = InMemoryQuotaService(
        quota_store=InMemoryTenantQuotaStore(),
        reservation_store=reservations,
    )
    result = await svc.reserve_tokens(
        ReserveRequest(
            tenant_id=tenant,
            agent="alpha",
            thread_id=uuid4(),
            estimated_tokens=1000,
        )
    )
    assert not result.granted
    assert result.reservation_id is None
    assert result.reason == "over_budget"


@pytest.mark.asyncio
async def test_store_reserve_rejects_even_when_caller_races_get_budget_then_reserve() -> None:
    """W1-PR2 Task 3 — TOCTOU regression at the store layer.

    ``asyncio``'s cooperative scheduler never preempts mid-coroutine: with
    no real I/O between a ``get_budget()`` read and a ``reserve()`` write,
    10 tasks fired via ``asyncio.gather`` simply run to completion one at a
    time (no interleaving), so a naive gather-based race test would pass
    even against the pre-fix code — it doesn't reproduce the hazard. The
    ``asyncio.sleep(0)`` below forces the real interleave point a
    multi-replica caller would see over the network (read budget, then
    later write): all 10 callers read the *same* stale ``reserved_total``
    before any of them writes. Before the fix, the store's ``reserve()``
    had no check of its own and trusted the caller — all 10 would win,
    landing ``reserved_total`` at 1000 (double the 500 budget). After the
    fix, ``reserve()``'s own atomic check (inside ``self._lock``) rejects
    5 of them regardless of what the racing caller believed, so exactly 5
    are admitted."""
    tenant = _tenant()
    store = InMemoryTokenReservationStore()
    month = datetime.now(tz=UTC).date().replace(day=1)
    await store.set_budget_total_for_test(tenant_id=tenant, month=month, budget_total=500)

    async def _racy_caller() -> bool:
        budget = await store.get_budget(tenant_id=tenant, month=month)
        assert budget is not None
        would_fit = budget.used_total + budget.reserved_total + 100 <= budget.budget_total
        await asyncio.sleep(0)  # force interleaving across the check→act window
        if not would_fit:
            return False
        try:
            await store.reserve(
                tenant_id=tenant, agent_name="alpha", thread_id=uuid4(), estimated=100
            )
        except BudgetExceededError:
            return False
        return True

    results = await asyncio.gather(*[_racy_caller() for _ in range(10)])
    assert sum(1 for granted in results if granted) == 5

    budget = await store.get_budget(tenant_id=tenant, month=month)
    assert budget is not None
    assert budget.reserved_total == 500  # exactly the cap — no overspend, no lost admit


@pytest.mark.asyncio
async def test_commit_marks_committed_and_updates_ledger() -> None:
    tenant = _tenant()
    reservations = InMemoryTokenReservationStore()
    svc = InMemoryQuotaService(
        quota_store=InMemoryTenantQuotaStore(),
        reservation_store=reservations,
    )
    reserve = await svc.reserve_tokens(
        ReserveRequest(
            tenant_id=tenant,
            agent="alpha",
            thread_id=uuid4(),
            estimated_tokens=400,
        )
    )
    assert reserve.granted and reserve.reservation_id is not None

    await svc.commit_tokens(
        CommitRequest(
            reservation_id=reserve.reservation_id,
            tenant_id=tenant,
            actual_tokens=350,
        )
    )

    row = await reservations.get(reservation_id=reserve.reservation_id, tenant_id=tenant)
    assert row is not None
    assert row.state is ReservationState.COMMITTED
    assert row.actual == 350

    month = datetime.now(tz=UTC).date().replace(day=1)
    budget = await reservations.get_budget(tenant_id=tenant, month=month)
    assert budget is not None
    assert budget.used_total == 350
    assert budget.reserved_total == 0  # refunded after commit


@pytest.mark.asyncio
async def test_release_refunds_reserved_total() -> None:
    tenant = _tenant()
    reservations = InMemoryTokenReservationStore()
    svc = InMemoryQuotaService(
        quota_store=InMemoryTenantQuotaStore(),
        reservation_store=reservations,
    )
    reserve = await svc.reserve_tokens(
        ReserveRequest(
            tenant_id=tenant,
            agent="alpha",
            thread_id=uuid4(),
            estimated_tokens=200,
        )
    )
    assert reserve.reservation_id is not None
    await svc.release_tokens(reserve.reservation_id, tenant_id=tenant)
    row = await reservations.get(reservation_id=reserve.reservation_id, tenant_id=tenant)
    assert row is not None
    assert row.state is ReservationState.RELEASED

    month = datetime.now(tz=UTC).date().replace(day=1)
    budget = await reservations.get_budget(tenant_id=tenant, month=month)
    assert budget is not None
    assert budget.used_total == 0
    assert budget.reserved_total == 0


# ---------------------------------------------------------------------------
# reservation reaper integration (via store)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_expired_finds_stale_reservations() -> None:
    """``list_expired`` underpins the reaper — verify it surfaces old rows."""
    from datetime import timedelta

    tenant = _tenant()
    store = InMemoryTokenReservationStore()
    # Seed a reservation, then back-date it by hand.
    row = await store.reserve(
        tenant_id=tenant,
        agent_name="alpha",
        thread_id=uuid4(),
        estimated=100,
    )
    # Replace with an old reserved_at so the cutoff catches it.
    old = row.model_copy(update={"reserved_at": row.reserved_at - timedelta(hours=1)})
    # Direct dict poke is reasonable here — we're testing the listing
    # primitive that production uses, the model_copy is the test
    # harness mechanism for "this row is stale".
    store._reservations[row.id] = old  # type: ignore[attr-defined]

    expired = await store.list_expired(max_age_seconds=600)  # 10min
    assert len(expired) == 1
    assert expired[0].id == row.id


@pytest.mark.asyncio
async def test_reaper_runs_once_releases_stale_rows() -> None:
    from datetime import timedelta

    from control_plane.quota import ReservationReaper

    tenant = _tenant()
    store = InMemoryTokenReservationStore()
    row = await store.reserve(
        tenant_id=tenant,
        agent_name="alpha",
        thread_id=uuid4(),
        estimated=100,
    )
    store._reservations[row.id] = row.model_copy(  # type: ignore[attr-defined]
        update={"reserved_at": row.reserved_at - timedelta(hours=1)}
    )

    reaper = ReservationReaper(
        reservation_store=store,
        max_age_s=600,
        interval_s=60,
    )
    released = await reaper.run_once()
    assert released == 1

    final = await store.get(reservation_id=row.id, tenant_id=tenant)
    assert final is not None
    assert final.state is ReservationState.EXPIRED


@pytest.mark.asyncio
async def test_reaper_invokes_on_expire_per_row() -> None:
    """The ``on_expire`` hook fires once per expired reservation with the
    row — this is what app.py wires to emit the
    ``quota:reservation_expired`` audit."""
    from datetime import timedelta

    from control_plane.quota import ReservationReaper
    from expert_work.protocol import TokenReservationRecord

    tenant = _tenant()
    store = InMemoryTokenReservationStore()
    row = await store.reserve(
        tenant_id=tenant,
        agent_name="alpha",
        thread_id=uuid4(),
        estimated=100,
    )
    store._reservations[row.id] = row.model_copy(  # type: ignore[attr-defined]
        update={"reserved_at": row.reserved_at - timedelta(hours=1)}
    )

    seen: list[TokenReservationRecord] = []

    async def _record(r: TokenReservationRecord) -> None:
        seen.append(r)

    reaper = ReservationReaper(
        reservation_store=store,
        max_age_s=600,
        interval_s=60,
        on_expire=_record,
    )
    released = await reaper.run_once()

    assert released == 1
    assert len(seen) == 1
    assert seen[0].id == row.id
    assert seen[0].tenant_id == tenant


@pytest.mark.asyncio
async def test_expire_reserved_is_exactly_once() -> None:
    """Stream 9.5 — two reapers racing the same stale row: only the first
    ``expire_reserved`` wins (True) + refunds; the loser gets False and the
    ledger is refunded exactly once (not driven negative)."""
    tenant = _tenant()
    store = InMemoryTokenReservationStore()
    row = await store.reserve(
        tenant_id=tenant,
        agent_name="alpha",
        thread_id=uuid4(),
        estimated=100,
    )

    first = await store.expire_reserved(reservation_id=row.id, tenant_id=tenant)
    second = await store.expire_reserved(reservation_id=row.id, tenant_id=tenant)
    assert first is True
    assert second is False

    final = await store.get(reservation_id=row.id, tenant_id=tenant)
    assert final is not None
    assert final.state is ReservationState.EXPIRED

    month = datetime.now(tz=UTC).date().replace(day=1)
    budget = await store.get_budget(tenant_id=tenant, month=month)
    assert budget is not None
    assert budget.reserved_total == 0  # refunded once — not -100


@pytest.mark.asyncio
async def test_expire_reserved_false_for_missing_or_cross_tenant() -> None:
    tenant = _tenant()
    store = InMemoryTokenReservationStore()
    row = await store.reserve(tenant_id=tenant, agent_name="alpha", thread_id=uuid4(), estimated=50)
    # Unknown id, and right id but wrong tenant → both no-op False.
    assert await store.expire_reserved(reservation_id=uuid4(), tenant_id=tenant) is False
    assert await store.expire_reserved(reservation_id=row.id, tenant_id=_tenant()) is False


@pytest.mark.asyncio
async def test_reaper_skips_hook_when_peer_won() -> None:
    """When ``expire_reserved`` returns False (a peer reaper / client closed the
    row first), the reaper must NOT fire ``on_expire`` and must NOT count it."""
    from datetime import timedelta

    from control_plane.quota import ReservationReaper
    from expert_work.protocol import TokenReservationRecord

    tenant = _tenant()
    real = InMemoryTokenReservationStore()
    row = await real.reserve(tenant_id=tenant, agent_name="alpha", thread_id=uuid4(), estimated=100)
    stale = row.model_copy(update={"reserved_at": row.reserved_at - timedelta(hours=1)})

    class _PeerWonStore:
        """Surfaces the stale row but reports every expire as already-lost."""

        async def list_expired(
            self, *, max_age_seconds: int, limit: int = 100
        ) -> list[TokenReservationRecord]:
            return [stale]

        async def expire_reserved(self, *, reservation_id: UUID, tenant_id: UUID) -> bool:
            return False

    seen: list[TokenReservationRecord] = []

    async def _record(r: TokenReservationRecord) -> None:
        seen.append(r)

    reaper = ReservationReaper(
        reservation_store=_PeerWonStore(),  # type: ignore[arg-type]
        max_age_s=600,
        interval_s=60,
        on_expire=_record,
    )
    released = await reaper.run_once()

    assert released == 0
    assert seen == []


@pytest.mark.asyncio
async def test_reaper_cycle_error_increments_metric() -> None:
    """A failing cycle bumps ``expert_work_control_plane_quota_reaper_cycle_errors_total``."""
    import asyncio

    from prometheus_client import REGISTRY

    from control_plane.quota import ReservationReaper

    metric = "expert_work_control_plane_quota_reaper_cycle_errors_total"

    class _RaisingStore:
        async def list_expired(self, **_kwargs: object) -> list[object]:
            msg = "store unavailable"
            raise RuntimeError(msg)

    before = REGISTRY.get_sample_value(metric) or 0.0

    # interval_s is large so exactly one cycle runs before stop().
    reaper = ReservationReaper(
        reservation_store=_RaisingStore(),  # type: ignore[arg-type]
        max_age_s=600,
        interval_s=3600,
    )
    reaper.start()
    await asyncio.sleep(0.05)  # let the first cycle hit the except branch
    await reaper.stop()

    after = REGISTRY.get_sample_value(metric) or 0.0
    assert after >= before + 1


# ---------------------------------------------------------------------------
# Mini-ADR J-30 (J.6.补强-1) — image upload dimensions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_upload_count_30d_denies_after_capacity() -> None:
    """``IMAGE_UPLOAD_COUNT_30D`` bucket exhausts after ``burst`` uploads
    when refill is the slow 30-day drip."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D,
            scope={},
            limit_value=3,  # 3 / 30d ≈ 1.16e-6 per second (slow drip)
            burst=3,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    for _ in range(3):
        result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
        assert result.allowed
    # 4th immediate upload exceeds capacity — bucket can't refill that fast.
    result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert not result.allowed
    assert result.blocked_dimension is QuotaDimension.IMAGE_UPLOAD_COUNT_30D


@pytest.mark.asyncio
async def test_image_storage_bytes_uses_cost_override() -> None:
    """``IMAGE_STORAGE_BYTES`` deducts the ``cost_overrides`` value
    (file_size) from the sticky bucket, not the default ``cost=1``."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES,
            scope={},
            limit_value=1024,  # 1 KiB ceiling
            burst=None,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    # First upload of 600 bytes — under ceiling.
    first = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 600},
        )
    )
    assert first.allowed
    assert first.remaining[QuotaDimension.IMAGE_STORAGE_BYTES.value] == 424

    # Second upload of 500 bytes — would push total to 1100 > 1024 ceiling.
    second = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 500},
        )
    )
    assert not second.allowed
    assert second.blocked_dimension is QuotaDimension.IMAGE_STORAGE_BYTES


@pytest.mark.asyncio
async def test_image_storage_bytes_does_not_refill() -> None:
    """Sticky-bucket semantic — once tokens are spent they stay spent
    until lifecycle deletion refunds them (J.6.补强-3 future scope)."""
    import asyncio

    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES,
            scope={},
            limit_value=100,
            burst=None,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    spend = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 100},
        )
    )
    assert spend.allowed

    await asyncio.sleep(0.01)  # Time passes — refill rate is 0, so nothing comes back.

    next_call = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 1},
        )
    )
    assert not next_call.allowed


# ---------------------------------------------------------------------------
# Mini-ADR J-25 (J.9-step2) — artifact dimensions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_download_count_30d_denies_after_capacity() -> None:
    """``ARTIFACT_DOWNLOAD_COUNT_30D`` exhausts after ``burst`` downloads
    on the same slow-drip 30-day refill shape as the image counterpart."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D,
            scope={},
            limit_value=2,
            burst=2,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    for _ in range(2):
        result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
        assert result.allowed
    denied = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert not denied.allowed
    assert denied.blocked_dimension is QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D


@pytest.mark.asyncio
async def test_artifact_storage_bytes_sticky_no_refill() -> None:
    """``ARTIFACT_STORAGE_BYTES`` mirrors ``IMAGE_STORAGE_BYTES``: sticky
    ceiling, spent tokens stay spent. Wired ahead of the save-side
    plumbing so the dimension is ready when orchestrator quota lands."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.ARTIFACT_STORAGE_BYTES,
            scope={},
            limit_value=1024,
            burst=None,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    first = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            cost_overrides={QuotaDimension.ARTIFACT_STORAGE_BYTES: 600},
        )
    )
    assert first.allowed
    assert first.remaining[QuotaDimension.ARTIFACT_STORAGE_BYTES.value] == 424

    second = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            cost_overrides={QuotaDimension.ARTIFACT_STORAGE_BYTES: 500},
        )
    )
    assert not second.allowed
    assert second.blocked_dimension is QuotaDimension.ARTIFACT_STORAGE_BYTES


# ---------------------------------------------------------------------------
# 沙箱迁移波 3 (spec § 3.1) — WORKSPACE_BYTES_PER_USER (storage-type cap,
# bucket ladder must stay inert)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_bytes_per_user_row_is_inert_in_bucket_engine() -> None:
    """WORKSPACE_BYTES_PER_USER 是存储型上限(spec § 3.1),bucket ladder 不认识它。

    limit_value=0 是哨兵:一个真正接进 ladder 的存储型维度(照 IMAGE_STORAGE_BYTES
    的样子,capacity=limit_value、refill=0)在 capacity=0 时对任何 cost>=1 的
    check 都必拒(0 < cost 恒真)。如果有人往 ladder 里加了这个维度的分支,
    这一行会立刻把 admission 打成拒绝,测试变红。(limit_value=1 曾试过但不够
    强——bucket 满载时 capacity==cost 恰好放行,哨兵咬不住;见变异自证记录。)
    """
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER,
            scope={},
            limit_value=0,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert result.allowed


# ---------------------------------------------------------------------------
# B-19 — resource_kind dimension routing + sticky Retry-After
# ---------------------------------------------------------------------------


async def _seed_image_dimensions(store: InMemoryTenantQuotaStore, tenant: UUID) -> None:
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D,
            scope={},
            limit_value=3,
            burst=3,
        ),
    )
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES,
            scope={},
            limit_value=1024,
            burst=None,
        ),
    )


@pytest.mark.asyncio
async def test_session_check_does_not_touch_image_buckets() -> None:
    """B-19 ① — a ``resource_kind="session"`` check must not spend from the
    image dimensions: their balances stay untouched for real uploads."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed_image_dimensions(store, tenant)
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    for _ in range(5):
        result = await svc.check(CheckRequest(tenant_id=tenant, cost=1, resource_kind="session"))
        assert result.allowed
        assert QuotaDimension.IMAGE_UPLOAD_COUNT_30D.value not in result.remaining
        assert QuotaDimension.IMAGE_STORAGE_BYTES.value not in result.remaining

    # The image buckets are still full: a real upload sees the whole budget.
    upload = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            resource_kind="image_upload",
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 1024},
        )
    )
    assert upload.allowed
    assert upload.remaining[QuotaDimension.IMAGE_UPLOAD_COUNT_30D.value] == 2
    assert upload.remaining[QuotaDimension.IMAGE_STORAGE_BYTES.value] == 0


@pytest.mark.asyncio
async def test_session_check_allowed_when_image_storage_exhausted() -> None:
    """B-19 ① production symptom — image storage full must NOT 429 a
    conversation (``resource_kind="run"``)."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES,
            scope={},
            limit_value=0,  # ceiling already fully consumed
            burst=None,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    result = await svc.check(CheckRequest(tenant_id=tenant, cost=1, resource_kind="run"))
    assert result.allowed


@pytest.mark.asyncio
async def test_image_upload_still_deducts_all_its_dimensions() -> None:
    """B-19 regression guard — ``resource_kind="image_upload"`` keeps
    deducting QPS + count + bytes exactly as before."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(dimension=QuotaDimension.QPS, scope={}, limit_value=10, burst=10),
    )
    await _seed_image_dimensions(store, tenant)
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    result = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            resource_kind="image_upload",
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 600},
        )
    )
    assert result.allowed
    assert result.remaining[QuotaDimension.QPS.value] == 9
    assert result.remaining[QuotaDimension.IMAGE_UPLOAD_COUNT_30D.value] == 2
    assert result.remaining[QuotaDimension.IMAGE_STORAGE_BYTES.value] == 424


@pytest.mark.asyncio
async def test_artifact_download_only_deducts_its_dimension() -> None:
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D,
            scope={},
            limit_value=2,
            burst=2,
        ),
    )
    await _seed_image_dimensions(store, tenant)
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    download = await svc.check(
        CheckRequest(tenant_id=tenant, cost=1, resource_kind="artifact_download")
    )
    assert download.allowed
    assert set(download.remaining) == {QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D.value}
    assert download.remaining[QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D.value] == 1

    # Image count bucket untouched by the download above.
    upload = await svc.check(CheckRequest(tenant_id=tenant, cost=1, resource_kind="image_upload"))
    assert upload.allowed
    assert upload.remaining[QuotaDimension.IMAGE_UPLOAD_COUNT_30D.value] == 2


@pytest.mark.asyncio
async def test_resource_kind_none_checks_all_dimensions() -> None:
    """Compat — direct callers that don't pass ``resource_kind`` keep the
    legacy behaviour: every configured dimension applies."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed_image_dimensions(store, tenant)
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    result = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 600},
        )
    )
    assert result.allowed
    assert result.remaining[QuotaDimension.IMAGE_UPLOAD_COUNT_30D.value] == 2
    assert result.remaining[QuotaDimension.IMAGE_STORAGE_BYTES.value] == 424


@pytest.mark.asyncio
async def test_artifact_storage_bytes_only_reachable_via_unfiltered_check() -> None:
    """B-19 — no admission surface deducts ``ARTIFACT_STORAGE_BYTES`` today
    (reserved for the future save-artifact path); only ``resource_kind=None``
    direct callers still hit it."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.ARTIFACT_STORAGE_BYTES,
            scope={},
            limit_value=0,
            burst=None,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    filtered = await svc.check(
        CheckRequest(tenant_id=tenant, cost=1, resource_kind="artifact_download")
    )
    assert filtered.allowed

    unfiltered = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert not unfiltered.allowed
    assert unfiltered.blocked_dimension is QuotaDimension.ARTIFACT_STORAGE_BYTES


@pytest.mark.asyncio
async def test_sticky_dimension_denial_has_no_retry_hint() -> None:
    """B-19 ② — refill=0 (sticky) dimensions have no time at which a retry
    would succeed → ``retry_after_s`` must be ``None``, not an astronomically
    large number."""
    tenant = _tenant()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES,
            scope={},
            limit_value=100,
            burst=None,
        ),
    )
    svc = InMemoryQuotaService(quota_store=store, reservation_store=InMemoryTokenReservationStore())

    denied = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            resource_kind="image_upload",
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 200},
        )
    )
    assert not denied.allowed
    assert denied.blocked_dimension is QuotaDimension.IMAGE_STORAGE_BYTES
    assert denied.retry_after_s is None


@pytest.mark.asyncio
async def test_refillable_dimension_denial_keeps_retry_hint() -> None:
    """B-19 ② non-regression — dimensions with a real refill keep their
    integer retry hint."""
    tenant = _tenant()
    quota_store, patch = _quota_store_with_qps(tenant, limit=1, burst=1)
    await _seed(quota_store, tenant, patch)
    svc = InMemoryQuotaService(
        quota_store=quota_store,
        reservation_store=InMemoryTokenReservationStore(),
    )

    assert (await svc.check(CheckRequest(tenant_id=tenant, cost=1))).allowed
    denied = await svc.check(CheckRequest(tenant_id=tenant, cost=1))
    assert not denied.allowed
    assert denied.retry_after_s is not None
    assert denied.retry_after_s >= 0
