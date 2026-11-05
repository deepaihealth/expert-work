"""B-267 —— profiles for knowledge bases ingested before profiles existed.

Nothing generates them on its own: a base gets a profile when a confirmed
ingest asks for one. This command lists every base without a profile with what
generating it would cost — **ids and numbers only, never names or content** —
and, with ``--apply``, asks for each one's refresh; the control-plane recovery
workers then run them (one auxiliary-model call each plus the probe
calibration, billed to the base's tenant as ``knowledge_ingest``). Report the
dry-run total to the user before applying (B-222: every spend is confirmed).

Run inside a control-plane pod (its settings and database)::

    python -m control_plane.knowledge.backfill_profiles            # dry-run
    python -m control_plane.knowledge.backfill_profiles --apply --expected-total 1.23

``--tenant`` / ``--kb`` narrow both modes to one tenant / one base; ``--apply``
recomputes the plan and refuses when its total is above ``--expected-total``
(the dry-run total the user confirmed) or anything is unpriced. Bases whose
profile was marked stale (a document deleted or switched off) are listed too.

``--apply`` refuses when the platform switch ``kb_first_routing`` is off:
nothing would run the refreshes.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from control_plane.knowledge.chunking import count_tokens
from control_plane.knowledge.pricing import PriceSource, price_tokens, split_model_key
from control_plane.knowledge.profile import (
    PROFILE_DETAILED_DOCUMENTS,
    PROFILE_MAX_HEADINGS,
    PROFILE_MAX_TITLES,
    PROFILE_OUTPUT_TOKENS_MAX,
    AuxModelChoice,
    calibration_tokens,
    profile_prompt,
    render_digest,
)
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.profiles import PROFILE_DEBOUNCE
from expert_work.protocol import KnowledgeBase

_PAGE: Final = 200


@dataclass(frozen=True)
class BaseBackfill:
    """What generating one base's profile is expected to cost."""

    tenant_id: UUID
    kb_id: UUID
    documents: int
    input_tokens: int
    output_tokens: int
    calibration_tokens: int
    #: Micro-yuan; ``None`` when a model has no price.
    micros: int | None

    def line(self) -> str:
        cost = "unpriced" if self.micros is None else f"{self.micros / 1_000_000:.4f}"
        return (
            f"tenant={self.tenant_id} kb={self.kb_id} documents={self.documents} "
            f"input_tokens={self.input_tokens} output_tokens={self.output_tokens} "
            f"calibration_tokens={self.calibration_tokens} cost_cny={cost}"
        )


async def _all_bases(store: KnowledgeStore) -> AsyncIterator[KnowledgeBase]:
    offset = 0
    while True:
        page, total = await store.list_bases_all_tenants_page(limit=_PAGE, offset=offset)
        for base in page:
            yield base
        offset += len(page)
        if not page or offset >= total:
            return


async def _priced(
    prices: PriceSource | None, model_key: str | None, input_tokens: int, output_tokens: int
) -> int | None:
    if model_key is None:
        return None
    provider, model = split_model_key(model_key)
    return await price_tokens(
        prices,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _wanted(base: KnowledgeBase, tenant_id: UUID | None, kb_id: UUID | None) -> bool:
    """In the filters, and without a profile that is shown (none, or stale)."""
    if tenant_id is not None and base.tenant_id != tenant_id:
        return False
    if kb_id is not None and base.id != kb_id:
        return False
    return base.profile is None or base.profile.shown_profile() is None


async def plan_backfill(
    store: KnowledgeStore,
    prices: PriceSource | None,
    *,
    aux: AuxModelChoice,
    tenant_id: UUID | None = None,
    kb_id: UUID | None = None,
) -> list[BaseBackfill]:
    """Every base (within the filters) without a profile it shows — none yet,
    or stale — with what one would cost (the prompt counted on its real
    digest; the answer at its cap). Caller bypasses row security."""
    plan: list[BaseBackfill] = []
    async for base in _all_bases(store):
        if not _wanted(base, tenant_id, kb_id):
            continue
        digest = await store.profile_digest(
            tenant_id=base.tenant_id,
            kb_id=base.id,
            max_titles=PROFILE_MAX_TITLES,
            detailed=PROFILE_DETAILED_DOCUMENTS,
            max_headings=PROFILE_MAX_HEADINGS,
        )
        rendered = render_digest(digest)
        if not rendered.represented:
            plan.append(BaseBackfill(base.tenant_id, base.id, 0, 0, 0, 0, 0))
            continue
        input_tokens = count_tokens(profile_prompt(rendered))
        embedding = (
            f"{base.embedding_provider}/{base.embedding_model}"
            if base.embedding_provider and base.embedding_model
            else None
        )
        calibration = calibration_tokens() if embedding is not None else 0
        chat = await _priced(prices, aux.key, input_tokens, PROFILE_OUTPUT_TOKENS_MAX)
        embed = await _priced(prices, embedding, calibration, 0) if calibration else 0
        plan.append(
            BaseBackfill(
                tenant_id=base.tenant_id,
                kb_id=base.id,
                documents=rendered.represented,
                input_tokens=input_tokens,
                output_tokens=PROFILE_OUTPUT_TOKENS_MAX,
                calibration_tokens=calibration,
                micros=None if chat is None or embed is None else chat + embed,
            )
        )
    return plan


async def apply_backfill(
    store: KnowledgeStore,
    plan: list[BaseBackfill],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Ask for the refresh of every planned base that has documents (due at
    once); return how many were asked."""
    asked = 0
    for entry in plan:
        if entry.documents == 0:
            continue
        if await store.mark_profile_refresh(
            tenant_id=entry.tenant_id, kb_id=entry.kb_id, now=now() - PROFILE_DEBOUNCE
        ):
            asked += 1
    return asked


def total_micros(plan: list[BaseBackfill]) -> int:
    """What the planned refreshes cost in all (priced ones)."""
    return sum(entry.micros for entry in plan if entry.documents and entry.micros is not None)


def within_expected(plan: list[BaseBackfill], expected_cny: float) -> bool:
    """``--apply`` only when the recomputed total is not above what the user
    was shown (and confirmed) — and nothing is unpriced, which would hide spend."""
    unpriced = any(entry.documents and entry.micros is None for entry in plan)
    return not unpriced and total_micros(plan) <= round(expected_cny * 1_000_000)


def summary(plan: list[BaseBackfill]) -> str:
    """The totals line — numbers only."""
    with_documents = [entry for entry in plan if entry.documents]
    priced = [entry.micros for entry in with_documents if entry.micros is not None]
    unpriced = len(with_documents) - len(priced)
    return (
        f"bases={len(with_documents)} skipped_empty={len(plan) - len(with_documents)} "
        f"total_cost_cny={math.fsum(priced) / 1_000_000:.4f} unpriced_bases={unpriced}"
    )


async def _amain(args: argparse.Namespace) -> int:  # pragma: no cover - needs a database
    from control_plane.knowledge.pricing import RateCardPrices
    from control_plane.settings import Settings
    from control_plane.tenant_scope import bypass_rls_session
    from expert_work.persistence import (
        DatabaseConfig,
        SqlKnowledgeStore,
        build_rls_sessionmaker,
        create_async_engine_from_config,
        create_async_session_factory,
    )
    from expert_work.persistence.billing.rate_card import DbModelRateCardStore

    settings = Settings()
    if args.apply and not settings.kb_first_routing:
        print("ERROR: kb_first_routing is off — no worker would run the refreshes")
        return 2
    if args.apply and args.expected_total is None:
        print("ERROR: --apply needs --expected-total (the dry-run total the user confirmed)")
        return 2
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=args.dsn or settings.db_dsn, pgbouncer_mode=settings.db_pgbouncer_mode)
    )
    try:
        session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
        store = SqlKnowledgeStore(session_factory)
        prices = RateCardPrices(DbModelRateCardStore(session_factory))
        aux = AuxModelChoice(
            provider=settings.memory_consolidator_default_aux_provider,
            model=settings.memory_consolidator_default_aux_model,
        )
        async with bypass_rls_session():
            plan = await plan_backfill(store, prices, aux=aux, tenant_id=args.tenant, kb_id=args.kb)
            for entry in plan:
                print(entry.line())
            print(summary(plan))
            if args.apply and not within_expected(plan, args.expected_total):
                print("ERROR: the total is above --expected-total (or unpriced); nothing asked")
                return 3
            if args.apply:
                print(f"asked={await apply_backfill(store, plan)}")
            else:
                print("dry-run (pass --apply to ask for the refreshes)")
    finally:
        await engine.dispose()
    return 0


def main() -> None:  # pragma: no cover - CLI entry
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="ask for the refreshes")
    parser.add_argument("--dsn", default=None, help="override the DB DSN")
    parser.add_argument("--tenant", type=UUID, default=None, help="only this tenant's bases")
    parser.add_argument("--kb", type=UUID, default=None, help="only this base")
    parser.add_argument(
        "--expected-total",
        type=float,
        default=None,
        help="--apply refuses when the recomputed total (CNY) is above this",
    )
    raise SystemExit(asyncio.run(_amain(parser.parse_args())))


if __name__ == "__main__":
    main()
