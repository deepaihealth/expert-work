"""B-267 —— the old-base profile backfill: dry-run prints ids and numbers only,
``--apply`` only asks for refreshes (the workers run them), bases that already
have a profile or have no document are left alone."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from control_plane.knowledge.backfill_profiles import (
    apply_backfill,
    plan_backfill,
    summary,
    total_micros,
    within_expected,
)
from control_plane.knowledge.chunking import count_tokens
from control_plane.knowledge.pricing import Rate
from control_plane.knowledge.profile import (
    PROFILE_OUTPUT_TOKENS_MAX,
    AuxModelChoice,
    calibration_tokens,
    profile_prompt,
    render_digest,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.profiles import PROFILE_DEBOUNCE
from expert_work.protocol import DocumentStatus, KnowledgeChunk
from expert_work.protocol.knowledge_profile import KnowledgeProfile

_AUX = AuxModelChoice(provider="acme", model="chat-1")
_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
#: Tenant text that must never reach the printout.
_SECRET_NAME = "机密客户库"
_SECRET_TEXT = "客户张三的保单号 ZX-0099"


@dataclass
class _Prices:
    async def rate(self, *, provider: str, model: str) -> Rate | None:
        return {
            ("acme", "chat-1"): Rate(2_000_000, 8_000_000),
            ("acme", "embed-1"): Rate(500_000, 0),
        }.get((provider, model))


async def _ready(store: InMemoryKnowledgeStore, tenant: UUID, kb_id: UUID, filename: str) -> None:
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename=filename, content=b"x"
    )
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document.id,
        chunk_index=0,
        content=_SECRET_TEXT,
        embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
    )
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[chunk])
    await store.set_document_status(
        tenant_id=tenant, document_id=document.id, status=DocumentStatus.READY, chunk_count=1
    )


async def _base(store: InMemoryKnowledgeStore, *, documents: int, name: str) -> tuple[UUID, UUID]:
    tenant = uuid4()
    base = await store.create_base(
        tenant_id=tenant, name=name, embedding_provider="acme", embedding_model="embed-1"
    )
    for number in range(documents):
        document = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"{_SECRET_NAME}{number}.md", content=b"x"
        )
        chunk = KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=0,
            content=_SECRET_TEXT,
            embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
        )
        await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[chunk])
        await store.set_document_status(
            tenant_id=tenant, document_id=document.id, status=DocumentStatus.READY, chunk_count=1
        )
    return tenant, base.id


async def test_dry_run_prints_ids_and_numbers_only() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = await _base(store, documents=2, name=_SECRET_NAME)
    plan = await plan_backfill(store, _Prices(), aux=_AUX)
    [entry] = plan
    digest = await store.profile_digest(
        tenant_id=tenant, kb_id=kb_id, max_titles=400, detailed=24, max_headings=3
    )
    input_tokens = count_tokens(profile_prompt(render_digest(digest)))
    expected = (input_tokens * 2_000_000 + PROFILE_OUTPUT_TOKENS_MAX * 8_000_000) // 1_000_000
    expected += calibration_tokens() * 500_000 // 1_000_000
    assert (entry.documents, entry.input_tokens, entry.micros) == (2, input_tokens, expected)
    printed = entry.line() + "\n" + summary(plan)
    assert _SECRET_NAME not in printed and "张三" not in printed and "ZX-0099" not in printed
    # Every field is an id or a number.
    for field in re.findall(r"(\w+)=(\S+)", printed):
        assert re.fullmatch(r"[0-9a-f-]{36}|[0-9.]+|unpriced", field[1]), field
    assert f"tenant={tenant} kb={kb_id} documents=2 " in printed


async def test_bases_with_a_profile_are_left_out_and_empty_ones_skipped() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = await _base(store, documents=1, name="a")
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=_NOW)
    [claim] = await store.claim_profile_refreshes(now=_NOW + PROFILE_DEBOUNCE, limit=1)
    profile = KnowledgeProfile(
        summary="s", generated_at=_NOW, source_doc_count=1, model="acme/chat-1"
    )
    await store.finish_profile_refresh(
        tenant_id=tenant,
        kb_id=kb_id,
        holder=claim.holder,
        marks=claim.marks,
        profile=profile,
        calibration=None,
        now=_NOW,
    )
    await _base(store, documents=0, name="b")
    plan = await plan_backfill(store, _Prices(), aux=_AUX)
    assert [entry.documents for entry in plan] == [0]
    assert summary(plan).startswith("bases=0 skipped_empty=1 ")
    # A stale profile (a document deleted) is listed again.
    [document] = await store.list_documents(tenant_id=tenant, kb_id=kb_id)
    await _ready(store, tenant, kb_id, "extra.md")
    await store.delete_document(tenant_id=tenant, document_id=document.id)
    plan = await plan_backfill(store, _Prices(), aux=_AUX)
    assert sorted(entry.documents for entry in plan) == [0, 1]


async def test_filters_narrow_to_a_tenant_or_a_base() -> None:
    store = InMemoryKnowledgeStore()
    tenant_a, kb_a = await _base(store, documents=1, name="a")
    tenant_b, kb_b = await _base(store, documents=1, name="b")
    assert [
        e.kb_id for e in await plan_backfill(store, _Prices(), aux=_AUX, tenant_id=tenant_b)
    ] == [kb_b]
    assert [e.kb_id for e in await plan_backfill(store, _Prices(), aux=_AUX, kb_id=kb_a)] == [kb_a]
    assert tenant_a != tenant_b


async def test_apply_is_refused_above_the_confirmed_total() -> None:
    store = InMemoryKnowledgeStore()
    await _base(store, documents=1, name="a")
    plan = await plan_backfill(store, _Prices(), aux=_AUX)
    shown = total_micros(plan) / 1_000_000
    assert within_expected(plan, shown)
    assert not within_expected(plan, shown - 0.0001)

    @dataclass
    class _None:
        async def rate(self, *, provider: str, model: str) -> Rate | None:
            return None

    unpriced = await plan_backfill(store, _None(), aux=_AUX)
    assert not within_expected(unpriced, 1000.0)


async def test_apply_asks_for_refreshes_due_at_once() -> None:
    store = InMemoryKnowledgeStore()
    await _base(store, documents=1, name="a")
    await _base(store, documents=0, name="b")
    plan = await plan_backfill(store, _Prices(), aux=_AUX)
    assert await apply_backfill(store, plan, now=lambda: _NOW) == 1
    [claim] = await store.claim_profile_refreshes(now=_NOW, limit=5)
    assert claim.calibrate_only is False


async def test_an_unpriced_model_says_so() -> None:
    @dataclass
    class _None:
        async def rate(self, *, provider: str, model: str) -> Rate | None:
            return None

    store = InMemoryKnowledgeStore()
    await _base(store, documents=1, name="a")
    [entry] = await plan_backfill(store, _None(), aux=_AUX)
    assert entry.micros is None
    assert entry.line().endswith("cost_cny=unpriced")
