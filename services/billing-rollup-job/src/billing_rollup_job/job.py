"""``BillingRollupJob`` — the Y4 cost-derivation sweep.

For a target month, derive the per-tenant billing ledger from the G.9
``token_usage`` meter priced by the Y3 ``model_rate_card``:

1.  Iterate every tenant (via ``TenantConfigStore.list_all``; read each tenant's
    ``plan``).
2.  Under that tenant's RLS scope, read its ``token_usage`` rows for
    ``[month_start, next_month_start)`` (windowed, no row cap).
3.  Price each row by the rate effective at *its own* ``observed_at`` (rates are
    temporally versioned). Provider is ``row.provider`` or — for legacy NULL
    rows — reverse-looked-up from ``MODEL_CATALOG``; an unknown / ambiguous
    model leaves the row **unpriced** (bucketed under ``provider="unknown"``,
    costs 0). Rate resolution runs under ``bypass_rls`` (rate card is a
    NULL-tenant platform table).
4.  Aggregate per ``(provider, model, agent_name)``: sum tokens + base + markup
    + billed.
5.  Upsert each bucket under the tenant's RLS scope.

Idempotency — **upsert-overwrite**: the ledger ``upsert`` is
``on_conflict_do_update`` on the bucket key, so re-running for a month
recomputes + overwrites every bucket rather than double-counting. Usage rows are
append-only, so a bucket never shrinks out of existence between runs; upsert
alone is sufficient (no delete-then-insert).

Integer money only — per-row ``base_micros`` is integer-summed, ``billed`` is
``apply_markup`` (integer), ``markup = billed - base`` (never recomputed by
division). Zero LLM/quota hot-path interaction — pure offline derivation.
"""

from __future__ import annotations

import calendar
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from expert_work.common.observability import expert_work_counter, expert_work_gauge
from expert_work.persistence import (
    ModelRateCardStore,
    TenantBillingLedgerStore,
    TenantConfigStore,
)
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.persistence.token_usage_store import (
    PLATFORM_OVERHEAD_USAGE_KIND,
    TokenUsageStore,
)
from expert_work.protocol import (
    TenantBillingLedgerRecord,
    TenantPlan,
    apply_markup,
    provider_for_model,
)

logger = logging.getLogger("expert_work.billing_rollup_job")

#: Provider key for usage rows whose provider could not be derived or priced.
UNKNOWN_PROVIDER = "unknown"

_unpriced_rows = expert_work_counter(
    "expert_work_billing_rollup_unpriced_rows_total",
    "token_usage rows the rollup could not price (no provider or no rate).",
)
_unpriced_buckets = expert_work_counter(
    "expert_work_billing_rollup_unpriced_buckets_total",
    "Ledger buckets written with priced=false.",
)
#: Stream Z-2 — rollup-computed billed cost (micro-元) per (tenant, model) for
#: the processed month. A gauge (SET each run) not a counter: the rollup
#: recomputes the whole month, so set-overwrite is idempotent across re-runs.
_billed_cost = expert_work_gauge(
    "expert_work_llm_billed_cost_micros",
    "Rollup-computed billed LLM cost (micro-元) per tenant+model for the month.",
    ("tenant", "model"),
)


# ---------------------------------------------------------------------------
# RLS scope helpers (mirror CurationWorker._bypass_rls / _tenant_scope)
# ---------------------------------------------------------------------------


@contextmanager
def _bypass_rls() -> Iterator[None]:
    """RLS-bypass scope — used for ``model_rate_card`` (NULL-tenant) reads."""
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@contextmanager
def _tenant_scope(tenant_id: UUID) -> Iterator[None]:
    """Scope a store call to ``tenant_id`` (tenant_billing_ledger / token_usage)."""
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    user = current_user_id_var.set(None)
    try:
        yield
    finally:
        current_user_id_var.reset(user)
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


# ---------------------------------------------------------------------------
# Month boundaries + bucket accumulator
# ---------------------------------------------------------------------------


def month_bounds(month: date) -> tuple[datetime, datetime]:
    """Return ``(month_start, next_month_start)`` as tz-aware UTC datetimes.

    ``month`` is normalized to its first day; the window is half-open
    ``[month_start, next_month_start)``.
    """
    first = month.replace(day=1)
    last_day = calendar.monthrange(first.year, first.month)[1]
    start = datetime(first.year, first.month, 1, tzinfo=UTC)
    end = datetime(first.year, first.month, last_day, tzinfo=UTC) + timedelta(days=1)
    return start, end


@dataclass
class _Bucket:
    """Mutable per-``(provider, model, agent_name)`` accumulator."""

    provider: str
    model: str
    agent_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    base_cost_micros: int = 0
    markup_cost_micros: int = 0
    billed_cost_micros: int = 0
    priced: bool = True


@dataclass(frozen=True)
class RollupReport:
    """Tally produced by one ``run_once`` sweep."""

    tenants_scanned: int = 0
    usage_rows_priced: int = 0
    usage_rows_unpriced: int = 0
    buckets_upserted: int = 0
    unpriced_buckets: int = 0
    duration_seconds: float = 0.0
    # Per-tenant bucket counts (observability).
    buckets_by_tenant: dict[str, int] = field(default_factory=dict)


class BillingRollupJob:
    """One-shot cost rollup → ``tenant_billing_ledger`` derivation."""

    def __init__(
        self,
        *,
        tenant_config_store: TenantConfigStore,
        token_usage_store: TokenUsageStore,
        rate_card_store: ModelRateCardStore,
        ledger_store: TenantBillingLedgerStore,
        tenant_page_size: int = 100,
    ) -> None:
        if tenant_page_size <= 0:
            msg = "tenant_page_size must be positive"
            raise ValueError(msg)
        self._tenants = tenant_config_store
        self._usage = token_usage_store
        self._rates = rate_card_store
        self._ledger = ledger_store
        self._tenant_page_size = tenant_page_size

    async def run_once(self, *, month: date) -> RollupReport:
        """Roll up ``month`` for every tenant. Idempotent (upsert-overwrite)."""
        started = time.monotonic()
        start, end = month_bounds(month)
        first_of_month = month.replace(day=1)

        tenants_scanned = 0
        rows_priced = 0
        rows_unpriced = 0
        buckets_total = 0
        unpriced_buckets = 0
        by_tenant: dict[str, int] = {}

        async for tenant_id, _plan in self._iter_tenants():
            tenants_scanned += 1
            buckets, p, u = await self._rollup_tenant(
                tenant_id=tenant_id,
                month=first_of_month,
                start=start,
                end=end,
            )
            rows_priced += p
            rows_unpriced += u
            if buckets:
                by_tenant[str(tenant_id)] = len(buckets)
            buckets_total += len(buckets)
            unpriced_buckets += sum(1 for b in buckets if not b.priced)

        return RollupReport(
            tenants_scanned=tenants_scanned,
            usage_rows_priced=rows_priced,
            usage_rows_unpriced=rows_unpriced,
            buckets_upserted=buckets_total,
            unpriced_buckets=unpriced_buckets,
            duration_seconds=time.monotonic() - started,
            buckets_by_tenant=by_tenant,
        )

    async def _iter_tenants(self) -> AsyncIterator[tuple[UUID, TenantPlan]]:
        # Pull every tenant page; ``list_all`` is the cross-tenant platform read,
        # so it runs under bypass (tenant_config is RLS-protected too).
        offset = 0
        while True:
            with _bypass_rls():
                page = await self._tenants.list_all(limit=self._tenant_page_size, offset=offset)
            if not page:
                return
            for cfg in page:
                yield cfg.tenant_id, cfg.plan
            if len(page) < self._tenant_page_size:
                return
            offset += self._tenant_page_size

    async def _rollup_tenant(
        self,
        *,
        tenant_id: UUID,
        month: date,
        start: datetime,
        end: datetime,
    ) -> tuple[list[_Bucket], int, int]:
        """Roll up one tenant's month; return (buckets, priced_rows, unpriced_rows)."""
        with _tenant_scope(tenant_id):
            rows = await self._usage.list_for_tenant_window(
                tenant_id=tenant_id, start=start, end=end
            )

        buckets: dict[tuple[str, str, str], _Bucket] = {}
        priced_rows = 0
        unpriced_rows = 0

        for row in rows:
            if row.observed_at is None:
                # Defensive: a windowed read never returns pre-insert rows.
                continue
            if row.usage_kind == PLATFORM_OVERHEAD_USAGE_KIND:
                # B-104 — the platform's own safety / retrieval overhead (judges,
                # rerank) is metered for ops visibility and the run's token
                # breaker, but it is the platform's cost, not the tenant's bill.
                continue
            provider = row.provider or provider_for_model(row.model)
            rate = None
            if provider is not None:
                with _bypass_rls():
                    rate = await self._rates.resolve(provider=provider, model=row.model)

            if provider is None or rate is None:
                unpriced_rows += 1
                _unpriced_rows.inc()
                logger.warning(
                    "billing_rollup.unpriced model=%s provider=%s reason=%s",
                    row.model,
                    provider if provider is not None else "<underived>",
                    "no_provider" if provider is None else "no_rate",
                )
                key = (UNKNOWN_PROVIDER, row.model, row.agent_name)
                bucket = buckets.get(key)
                if bucket is None:
                    bucket = _Bucket(
                        provider=UNKNOWN_PROVIDER,
                        model=row.model,
                        agent_name=row.agent_name,
                        priced=False,
                    )
                    buckets[key] = bucket
                bucket.input_tokens += row.input_tokens
                bucket.output_tokens += row.output_tokens
                bucket.cache_creation_tokens += row.cache_creation_tokens
                bucket.cache_read_tokens += row.cache_read_tokens
                continue

            priced_rows += 1
            # Prices are micro-元 / 百万 tokens; divide once at the end to reach
            # the ledger's per-token micro-元 (floor, matching apply_markup).
            base_micros = (
                row.input_tokens * rate.input_per_mtok_micros
                + row.output_tokens * rate.output_per_mtok_micros
                + row.cache_creation_tokens * rate.cache_creation_per_mtok_micros
                + row.cache_read_tokens * rate.cache_read_per_mtok_micros
            ) // 1_000_000
            # Markup moved to tenant scope (separate PR); 0 here → billed == base.
            billed_micros = apply_markup(base_micros, 0)
            markup_micros = billed_micros - base_micros

            key = (provider, row.model, row.agent_name)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _Bucket(provider=provider, model=row.model, agent_name=row.agent_name)
                buckets[key] = bucket
            bucket.input_tokens += row.input_tokens
            bucket.output_tokens += row.output_tokens
            bucket.cache_creation_tokens += row.cache_creation_tokens
            bucket.cache_read_tokens += row.cache_read_tokens
            bucket.base_cost_micros += base_micros
            bucket.markup_cost_micros += markup_micros
            bucket.billed_cost_micros += billed_micros

        priced_at = datetime.now(tz=UTC)
        ordered = list(buckets.values())
        # Stream Z-2 cost metric: SET (not inc) the billed total per (tenant,
        # model) — the rollup recomputes the whole month, so a set is idempotent
        # across re-runs where an inc would double-count. Label cardinality is
        # tenant x model (no agent_name) by design.
        billed_by_model: dict[str, int] = defaultdict(int)
        for bucket in ordered:
            if not bucket.priced:
                _unpriced_buckets.inc()
            billed_by_model[bucket.model] += bucket.billed_cost_micros
        for model, billed in billed_by_model.items():
            _billed_cost.labels(tenant=str(tenant_id), model=model).set(billed)
        for bucket in ordered:
            record = TenantBillingLedgerRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                month=month,
                provider=bucket.provider,
                model=bucket.model,
                agent_name=bucket.agent_name,
                input_tokens=bucket.input_tokens,
                output_tokens=bucket.output_tokens,
                cache_creation_tokens=bucket.cache_creation_tokens,
                cache_read_tokens=bucket.cache_read_tokens,
                base_cost_micros=bucket.base_cost_micros,
                markup_cost_micros=bucket.markup_cost_micros,
                billed_cost_micros=bucket.billed_cost_micros,
                priced=bucket.priced,
                rate_card_priced_at=priced_at,
                created_at=priced_at,
                updated_at=priced_at,
            )
            with _tenant_scope(tenant_id):
                await self._ledger.upsert(record)

        return ordered, priced_rows, unpriced_rows


__all__ = [
    "UNKNOWN_PROVIDER",
    "BillingRollupJob",
    "RollupReport",
    "month_bounds",
]
