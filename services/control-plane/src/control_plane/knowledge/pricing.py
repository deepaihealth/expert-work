"""B-222 PR-2 —— what knowledge-ingest tokens cost, the way the bill prices them.

Prices come from the platform rate card (``model_rate_card``: micro-yuan per
million tokens, one row per ``provider/model``); a usage row costs
``(input * input rate + output * output rate) // 1_000_000``, then the tenant's
markup — none yet, as in the billing rollup. A model without a rate card row
is **unpriced**: not billed, so its line costs 0 and says so. Nothing here
names a vendor.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from control_plane.tenant_scope import bypass_rls_session
from expert_work.persistence.billing.rate_card import ModelRateCardStore
from expert_work.protocol.billing import apply_markup, provider_for_model
from expert_work.protocol.knowledge_estimate import (
    ActualItem,
    IngestActual,
    IngestOutcome,
    token_micros,
)

#: The tenant markup the billing rollup applies today (basis points).
TENANT_MARKUP_BPS: Final = 0


@dataclass(frozen=True)
class Rate:
    """Micro-yuan per million tokens."""

    input_per_mtok_micros: int
    output_per_mtok_micros: int


class PriceSource(Protocol):
    async def rate(self, *, provider: str, model: str) -> Rate | None:
        """The model's price, or ``None`` (no rate card row: unpriced)."""


@dataclass(frozen=True)
class RateCardPrices:
    """:class:`PriceSource` reading the platform rate card (a platform table:
    read with row security bypassed, as every rate card read)."""

    store: ModelRateCardStore

    async def rate(self, *, provider: str, model: str) -> Rate | None:
        async with bypass_rls_session():
            record = await self.store.resolve(provider=provider, model=model)
        if record is None:
            return None
        return Rate(record.input_per_mtok_micros, record.output_per_mtok_micros)


def split_model_key(key: str) -> tuple[str | None, str]:
    """``provider/model`` (an embedding key may carry ``#<marker>``) → its
    provider and model; a bare model name → the catalog's single owner."""
    bare = key.split("#", 1)[0]
    provider, sep, model = bare.partition("/")
    if sep and provider and model:
        return provider, model
    return provider_for_model(bare), bare


async def price_spend(
    prices: PriceSource | None,
    rows: Sequence[tuple[str | None, str, int, int]],
    *,
    estimate_id: UUID | None,
    generation: int,
    outcome: IngestOutcome,
    now: datetime,
) -> IngestActual:
    """What an ingest's usage rows (``provider``, ``model``, input, output) were
    billed: each row priced on its own (the bill's rounding), added up per
    model; a model without a price is unpriced."""
    rates: dict[tuple[str, str], Rate | None] = {}
    totals: dict[tuple[str | None, str], list[int]] = {}
    unpriced: set[tuple[str | None, str]] = set()
    for recorded_provider, model, input_tokens, output_tokens in rows:
        provider = recorded_provider or provider_for_model(model)
        rate: Rate | None = None
        if prices is not None and provider is not None:
            if (provider, model) not in rates:
                rates[(provider, model)] = await prices.rate(provider=provider, model=model)
            rate = rates[(provider, model)]
        held = totals.setdefault((provider, model), [0, 0, 0])
        held[0] += input_tokens
        held[1] += output_tokens
        if rate is None:
            unpriced.add((provider, model))
            continue
        base = token_micros(
            input_tokens, output_tokens, rate.input_per_mtok_micros, rate.output_per_mtok_micros
        )
        held[2] += apply_markup(base, TENANT_MARKUP_BPS)
    items = tuple(
        ActualItem(
            provider=provider,
            model=model,
            input_tokens=held[0],
            output_tokens=held[1],
            micros=held[2],
            priced=(provider, model) not in unpriced,
        )
        for (provider, model), held in totals.items()
    )
    return IngestActual(
        estimate_id=estimate_id,
        generation=generation,
        total_micros=sum(item.micros for item in items),
        unpriced=bool(unpriced),
        items=items,
        outcome=outcome,
        finished_at=now,
    )


async def price_tokens(
    prices: PriceSource | None,
    *,
    provider: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> int | None:
    """What the tokens are billed (micro-yuan), or ``None`` when the model is
    unpriced (no price source, no provider, no rate card row)."""
    if prices is None or provider is None:
        return None
    rate = await prices.rate(provider=provider, model=model)
    if rate is None:
        return None
    base = token_micros(
        input_tokens, output_tokens, rate.input_per_mtok_micros, rate.output_per_mtok_micros
    )
    return apply_markup(base, TENANT_MARKUP_BPS)
