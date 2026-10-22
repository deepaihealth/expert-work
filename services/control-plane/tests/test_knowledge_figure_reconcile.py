"""B-209 PR-2 (review H5 / security review S-M2) —— the original-figure
reconciler deletes originals whose document row is gone."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.recognition import figure_reconcile
from control_plane.knowledge.recognition.figure_reconcile import FigureReconciler
from control_plane.knowledge.recognition.figure_storage import FigureStorage
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.runtime.storage import InMemoryObjectStore

_FIGURE = "0123456789abcdef"


class _Store(InMemoryKnowledgeStore):
    """Records the RLS context and the batches of each lookup."""

    def __init__(self) -> None:
        super().__init__()
        self.lookups: list[tuple[bool, UUID | None, int]] = []

    async def existing_document_ids(self, *, document_ids: Sequence[UUID]) -> set[UUID]:
        self.lookups.append((bypass_rls_var.get(), current_tenant_id_var.get(), len(document_ids)))
        return await super().existing_document_ids(document_ids=document_ids)


async def _setup() -> tuple[_Store, InMemoryObjectStore, FigureStorage, UUID, UUID, UUID]:
    store, objects = _Store(), InMemoryObjectStore()
    storage = FigureStorage(objects)
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    kept = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.pdf", content=b"a"
    )
    gone = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="b.pdf", content=b"b"
    )
    for document, generation in ((kept.id, 0), (gone.id, 0), (gone.id, 1)):
        await storage.put(
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document,
            generation=generation,
            figure=_FIGURE,
            image=b"jpeg",
        )
    assert await store.delete_document(tenant_id=tenant, document_id=gone.id)
    return store, objects, storage, tenant, kept.id, gone.id


async def test_originals_of_a_deleted_document_are_deleted() -> None:
    store, objects, storage, _, kept, _ = await _setup()
    unrelated = ["knowledge_figures/notes.txt", "other/prefix/x.jpg"]
    for key in unrelated:
        await objects.put(key, b"x")
    deleted = await FigureReconciler(store=store, storage=storage, interval_s=60).run_once()
    assert deleted == 2
    remaining = await objects.list_prefix("")
    figures = [key for key in remaining if key not in unrelated]
    assert len(remaining) == 3 and len(figures) == 1
    assert f"/{kept}/g0/{_FIGURE}.jpg" in figures[0]


async def test_the_lookup_spans_tenants_and_runs_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, storage, _, _, _ = await _setup()
    monkeypatch.setattr(figure_reconcile, "_LOOKUP_BATCH", 1)
    token = current_tenant_id_var.set(uuid4())
    try:
        await FigureReconciler(store=store, storage=storage, interval_s=60).run_once()
    finally:
        current_tenant_id_var.reset(token)
    assert store.lookups == [(True, None, 1), (True, None, 1)]
    assert bypass_rls_var.get() is False


async def test_nothing_stored_means_no_lookup() -> None:
    store = _Store()
    reconciler = FigureReconciler(
        store=store, storage=FigureStorage(InMemoryObjectStore()), interval_s=60
    )
    assert await reconciler.run_once() == 0
    assert store.lookups == []


async def test_the_loop_runs_a_pass_and_survives_a_failing_one() -> None:
    store, objects, storage, *_ = await _setup()
    passes: list[int] = []

    class _Flaky(FigureReconciler):
        async def run_once(self) -> int:
            passes.append(1)
            if len(passes) == 1:
                raise RuntimeError("object store down")
            return await super().run_once()

    reconciler = _Flaky(store=store, storage=storage, interval_s=0.01)
    reconciler.start()
    for _ in range(200):
        if len(passes) >= 2:
            break
        await asyncio.sleep(0.01)
    await reconciler.stop()
    assert len(passes) >= 2
    assert len(await objects.list_prefix("")) == 1


class _Batching(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.single: list[str] = []
        self.batches: list[int] = []

    async def delete(self, key: str) -> None:
        self.single.append(key)
        await super().delete(key)

    async def delete_many(self, keys: Sequence[str]) -> None:
        self.batches.append(len(keys))
        await super().delete_many(keys)


async def test_deletes_go_out_in_batches_when_the_store_can() -> None:
    # Review M8: one ``DeleteObjects`` call, not one request per original.
    objects = _Batching()
    storage = FigureStorage(objects)
    tenant, kb, document = uuid4(), uuid4(), uuid4()
    for generation in range(3):
        await storage.put(
            tenant_id=tenant,
            kb_id=kb,
            document_id=document,
            generation=generation,
            figure=_FIGURE,
            image=b"jpeg",
        )
    assert await storage.delete_document(tenant_id=tenant, kb_id=kb, document_id=document) == 3
    assert (objects.batches, objects.single) == ([3], [])
