"""Unit tests for :class:`BillingRollupJob` — Stream Y4 cost derivation.

Covers: basic pricing (billed == base, markup 0), idempotency (re-run does not
double-count), decimal per-million-token price precision, provider derivation
(NULL provider resolved via MODEL_CATALOG; unknown model → unpriced), and
missing-rate → unpriced.

All in-memory stores; the RLS contextvars the job sets are no-ops for them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

import expert_work.protocol.billing as billing_mod
from billing_rollup_job.job import UNKNOWN_PROVIDER, BillingRollupJob, month_bounds
from expert_work.persistence import (
    InMemoryModelRateCardStore,
    InMemoryTenantBillingLedgerStore,
)
from expert_work.persistence.tenant_config.memory import InMemoryTenantConfigStore
from expert_work.persistence.token_usage_store import (
    PLATFORM_OVERHEAD_USAGE_KIND,
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)
from expert_work.protocol import ModelRateCardUpsert, TenantPlan

MONTH = date(2026, 6, 1)


@pytest.fixture
def tenants() -> InMemoryTenantConfigStore:
    return InMemoryTenantConfigStore()


@pytest.fixture
def usage() -> InMemoryTokenUsageStore:
    return InMemoryTokenUsageStore()


@pytest.fixture
def rates() -> InMemoryModelRateCardStore:
    return InMemoryModelRateCardStore()


@pytest.fixture
def ledger() -> InMemoryTenantBillingLedgerStore:
    return InMemoryTenantBillingLedgerStore()


def _job(
    tenants: InMemoryTenantConfigStore,
    usage: InMemoryTokenUsageStore,
    rates: InMemoryModelRateCardStore,
    ledger: InMemoryTenantBillingLedgerStore,
) -> BillingRollupJob:
    return BillingRollupJob(
        tenant_config_store=tenants,
        token_usage_store=usage,
        rate_card_store=rates,
        ledger_store=ledger,
    )


async def _add_tenant(tenants: InMemoryTenantConfigStore, *, plan: TenantPlan) -> UUID:
    tenant_id = uuid4()
    await tenants.create(tenant_id=tenant_id, display_name="acme", plan=plan, actor_id="test")
    return tenant_id


async def _add_usage(
    usage: InMemoryTokenUsageStore,
    *,
    tenant_id: UUID,
    model: str,
    observed_at: datetime,
    provider: str | None = None,
    agent_name: str = "support",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    usage_kind: str = "conversation",
) -> None:
    stored = await usage.insert(
        TokenUsageRecord(
            tenant_id=tenant_id,
            agent_name=agent_name,
            agent_version="1.0.0",
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            usage_kind=usage_kind,
        )
    )
    # Pin observed_at (insert() stamps now()).
    usage._rows[-1] = replace(stored, observed_at=observed_at)


async def _add_rate(
    rates: InMemoryModelRateCardStore,
    *,
    provider: str,
    model: str,
    input_micros: int,
    output_micros: int = 0,
) -> None:
    # ``input_micros`` is the legacy per-token micro figure; prices are now stored
    # per *million* tokens, so *1e6 keeps the rollup's base math identical (it
    # divides by 1e6). billed == base (markup moved to tenant scope).
    await rates.create(
        upsert=ModelRateCardUpsert(
            provider=provider,
            model=model,
            input_per_mtok_micros=input_micros * 1_000_000,
            output_per_mtok_micros=output_micros * 1_000_000,
        ),
        actor_id="test",
    )


# ---------------------------------------------------------------------------
# month_bounds
# ---------------------------------------------------------------------------


def test_month_bounds_half_open() -> None:
    start, end = month_bounds(date(2026, 6, 15))
    assert start == datetime(2026, 6, 1, tzinfo=UTC)
    assert end == datetime(2026, 7, 1, tzinfo=UTC)


def test_month_bounds_december_rolls_year() -> None:
    start, end = month_bounds(date(2026, 12, 1))
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# (a) basic pricing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_pricing(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    at = datetime(2026, 6, 10, tzinfo=UTC)
    # base = 1000*15 + 500*75 = 15000 + 37500 = 52500; billed = base (markup 0)
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider="anthropic",
        observed_at=at,
        input_tokens=1000,
        output_tokens=500,
    )
    await _add_rate(
        rates,
        provider="anthropic",
        model="claude-opus-4-8",
        input_micros=15,
        output_micros=75,
    )

    report = await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    assert report.usage_rows_priced == 1
    assert report.usage_rows_unpriced == 0

    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert len(rows) == 1
    bucket = rows[0]
    assert bucket.priced is True
    assert bucket.base_cost_micros == 52_500
    assert bucket.billed_cost_micros == 52_500  # billed == base (markup 0)
    assert bucket.markup_cost_micros == 0


@pytest.mark.asyncio
async def test_platform_overhead_rows_stay_off_the_tenant_bill(
    tenants: InMemoryTenantConfigStore,
    usage: InMemoryTokenUsageStore,
    rates: InMemoryModelRateCardStore,
    ledger: InMemoryTenantBillingLedgerStore,
) -> None:
    # B-104 — judges / rerank spend is metered as ``platform_overhead``: visible to
    # ops and to the run's token breaker, but it is the platform's cost.
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    at = datetime(2026, 6, 10, tzinfo=UTC)
    for kind, tokens in (("conversation", 1000), (PLATFORM_OVERHEAD_USAGE_KIND, 700)):
        await _add_usage(
            usage,
            tenant_id=tenant,
            model="claude-opus-4-8",
            provider="anthropic",
            observed_at=at,
            input_tokens=tokens,
            usage_kind=kind,
        )
    await _add_rate(rates, provider="anthropic", model="claude-opus-4-8", input_micros=15)

    report = await _job(tenants, usage, rates, ledger).run_once(month=MONTH)

    assert report.usage_rows_priced == 1
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert [(r.input_tokens, r.base_cost_micros) for r in rows] == [(1000, 15_000)]


# ---------------------------------------------------------------------------
# (b) idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_rerun_does_not_double_count(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.FREE)
    at = datetime(2026, 6, 5, tzinfo=UTC)
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider="anthropic",
        observed_at=at,
        input_tokens=100,
    )
    await _add_rate(
        rates,
        provider="anthropic",
        model="claude-opus-4-8",
        input_micros=10,
    )
    job = _job(tenants, usage, rates, ledger)

    await job.run_once(month=MONTH)
    await job.run_once(month=MONTH)

    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert len(rows) == 1  # not doubled
    assert rows[0].base_cost_micros == 1000  # 100 * 10, not 2000


# ---------------------------------------------------------------------------
# (c) decimal price precision (per-million-tokens granularity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decimal_price_per_mtok(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    # ¥0.5 / 百万tokens = 500_000 micro-元/百万token. 1000 tokens →
    # 1000 * 500_000 // 1e6 = 500 micro-元 = ¥0.0005.
    await rates.create(
        upsert=ModelRateCardUpsert(
            provider="anthropic",
            model="claude-opus-4-8",
            input_per_mtok_micros=500_000,
            output_per_mtok_micros=0,
        ),
        actor_id="test",
    )
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider="anthropic",
        observed_at=datetime(2026, 6, 10, tzinfo=UTC),
        input_tokens=1000,
    )
    await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert rows[0].base_cost_micros == 500


# ---------------------------------------------------------------------------
# (d) provider derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_derived_from_model_when_null(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    # provider=None on the usage row → reverse-resolved via MODEL_CATALOG.
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider=None,
        observed_at=datetime(2026, 6, 10, tzinfo=UTC),
        input_tokens=100,
    )
    await _add_rate(
        rates,
        provider="anthropic",
        model="claude-opus-4-8",
        input_micros=10,
    )
    report = await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    assert report.usage_rows_priced == 1
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert rows[0].provider == "anthropic"
    assert rows[0].priced is True


@pytest.mark.asyncio
async def test_unknown_model_is_unpriced(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="totally-unknown-model",
        provider=None,
        observed_at=datetime(2026, 6, 10, tzinfo=UTC),
        input_tokens=100,
    )
    report = await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    assert report.usage_rows_unpriced == 1
    assert report.usage_rows_priced == 0
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert len(rows) == 1
    bucket = rows[0]
    assert bucket.provider == UNKNOWN_PROVIDER
    assert bucket.priced is False
    assert bucket.input_tokens == 100  # tokens still recorded
    assert bucket.base_cost_micros == 0
    assert bucket.billed_cost_micros == 0


@pytest.mark.asyncio
async def test_ambiguous_model_is_unpriced(
    tenants, usage, rates, ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model registered under >1 provider resolves to None (ambiguous) — a
    # distinct code path from an entirely-unknown model, but the same unpriced
    # outcome. Force ambiguity by mapping a real model name to None in the index.
    monkeypatch.setitem(billing_mod._MODEL_PROVIDER_INDEX, "claude-opus-4-8", None)
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider=None,  # underivable → ambiguity path
        observed_at=datetime(2026, 6, 10, tzinfo=UTC),
        input_tokens=100,
    )
    report = await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    assert report.usage_rows_unpriced == 1
    assert report.usage_rows_priced == 0
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert len(rows) == 1
    assert rows[0].provider == UNKNOWN_PROVIDER
    assert rows[0].priced is False
    assert rows[0].input_tokens == 100  # tokens preserved


# ---------------------------------------------------------------------------
# (e) no rate for (provider, model) → unpriced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_matching_rate_is_unpriced(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    # Provider derivable, but no rate card row exists for it.
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider="anthropic",
        observed_at=datetime(2026, 6, 10, tzinfo=UTC),
        input_tokens=100,
    )
    report = await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    assert report.usage_rows_unpriced == 1
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert len(rows) == 1
    assert rows[0].provider == UNKNOWN_PROVIDER
    assert rows[0].priced is False


@pytest.mark.asyncio
async def test_priced_and_unpriced_rows_separate_buckets(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    await _add_rate(
        rates,
        provider="anthropic",
        model="claude-opus-4-8",
        input_micros=10,
    )
    # one priced (anthropic) + one unpriced (unknown model)
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider="anthropic",
        observed_at=datetime(2026, 6, 10, tzinfo=UTC),
        input_tokens=100,
    )
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="mystery-model",
        provider=None,
        observed_at=datetime(2026, 6, 11, tzinfo=UTC),
        input_tokens=50,
    )
    await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert len(rows) == 2
    by_provider = {r.provider: r for r in rows}
    assert by_provider["anthropic"].priced is True
    assert by_provider[UNKNOWN_PROVIDER].priced is False


@pytest.mark.asyncio
async def test_excludes_rows_outside_month(tenants, usage, rates, ledger) -> None:
    tenant = await _add_tenant(tenants, plan=TenantPlan.PRO)
    await _add_rate(
        rates,
        provider="anthropic",
        model="claude-opus-4-8",
        input_micros=10,
    )
    # In June (counted) + in July (excluded for a June rollup).
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider="anthropic",
        observed_at=datetime(2026, 6, 30, 23, 59, tzinfo=UTC),
        input_tokens=100,
    )
    await _add_usage(
        usage,
        tenant_id=tenant,
        model="claude-opus-4-8",
        provider="anthropic",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        input_tokens=999,
    )
    await _job(tenants, usage, rates, ledger).run_once(month=MONTH)
    rows = await ledger.list_for_tenant(tenant_id=tenant, month=MONTH)
    assert len(rows) == 1
    assert rows[0].input_tokens == 100  # July row excluded
