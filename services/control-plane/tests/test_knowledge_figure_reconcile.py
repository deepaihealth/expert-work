"""B-209 PR-2 (review H5 / security review S-M2 / re-review H1) —— the
original-figure reconciler deletes originals whose document row is gone, and
only when that "gone" can be trusted."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.advisory_locks import FIGURE_RECONCILE_LOCK_CLASSID
from control_plane.knowledge.recognition import figure_reconcile
from control_plane.knowledge.recognition.figure_reconcile import (
    FigureReconciler,
    ReconcileOutcome,
    build_figure_reconciler,
)
from control_plane.knowledge.recognition.figure_storage import FigureStorage, parse_key
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge import SqlKnowledgeStore
from expert_work.persistence.knowledge.base import RowSecurityActiveError
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.runtime.storage import InMemoryObjectStore
from tests.fake_advisory_lock import FakeAdvisoryLockSessionFactory

_FIGURE = "0123456789abcdef"


def _later() -> datetime:
    """A clock a day and an hour ahead: everything stored now is old enough."""
    return datetime.now(UTC) + timedelta(hours=25)


class _Store(InMemoryKnowledgeStore):
    """Proves (or not) that it sees every tenant; records each lookup's RLS
    context and batch size."""

    def __init__(self, *, sees_all: bool = True) -> None:
        super().__init__()
        self.sees_all = sees_all
        self.checked_with_bypass: list[bool] = []
        self.lookups: list[tuple[bool, UUID | None, int]] = []
        #: The lookup that raises (1-based), and what it raises.
        self.fail_at: tuple[int, Exception] | None = None

    async def sees_every_tenant(self) -> bool:
        self.checked_with_bypass.append(bypass_rls_var.get())
        return self.sees_all

    async def existing_document_ids(self, *, document_ids: Sequence[UUID]) -> set[UUID]:
        self.lookups.append((bypass_rls_var.get(), current_tenant_id_var.get(), len(document_ids)))
        if self.fail_at is not None and len(self.lookups) == self.fail_at[0]:
            raise self.fail_at[1]
        return await super().existing_document_ids(document_ids=document_ids)


class _Setup:
    def __init__(self, *, sees_all: bool = True, objects: InMemoryObjectStore | None = None):
        self.store = _Store(sees_all=sees_all)
        self.objects = objects or InMemoryObjectStore()
        self.storage = FigureStorage(self.objects)
        self.tenant = uuid4()

    async def documents(self, kept: int, gone: int) -> tuple[list[UUID], list[UUID]]:
        """``kept`` live documents and ``gone`` deleted ones, one original each."""
        base = await self.store.create_base(tenant_id=self.tenant, name=f"kb-{uuid4().hex[:6]}")
        made: list[UUID] = []
        for n in range(kept + gone):
            document = await self.store.upsert_document(
                tenant_id=self.tenant, kb_id=base.id, filename=f"{n}.pdf", content=b"%PDF"
            )
            await self.storage.put(
                tenant_id=self.tenant,
                kb_id=base.id,
                document_id=document.id,
                generation=0,
                figure=_FIGURE,
                image=b"jpeg",
            )
            made.append(document.id)
        for document_id in made[kept:]:
            assert await self.store.delete_document(tenant_id=self.tenant, document_id=document_id)
        return made[:kept], made[kept:]

    def reconciler(self, **kwargs: object) -> FigureReconciler:
        options: dict[str, object] = {"interval_s": 60, "delete": True, "now": _later}
        options.update(kwargs)
        return FigureReconciler(store=self.store, storage=self.storage, **options)  # type: ignore[arg-type]

    async def holders(self) -> set[str]:
        """Document ids that still have an original."""
        keys = await self.objects.list_prefix("knowledge_figures/")
        return {str(parsed.document_id) for parsed in map(parse_key, keys) if parsed is not None}


async def test_originals_of_a_deleted_document_are_deleted() -> None:
    setup = _Setup()
    kept, _ = await setup.documents(kept=2, gone=1)
    unrelated = ["knowledge_figures/notes.txt", "other/prefix/x.jpg"]
    for key in unrelated:
        await setup.objects.put(key, b"x")
    outcome = await setup.reconciler().run_once()
    assert outcome == ReconcileOutcome(orphans=1, deleted=1)
    assert await setup.holders() == {str(d) for d in kept}
    remaining = await setup.objects.list_prefix("")
    assert all(key in remaining for key in unrelated)


async def test_a_key_that_only_looks_like_a_figure_never_stops_a_pass() -> None:
    # Re-review L3: 36 characters of hex and hyphens that are no UUID.
    setup = _Setup()
    await setup.documents(kept=2, gone=1)
    odd = "-" * 36
    bad = f"knowledge_figures/{odd}/{odd}/{odd}/g0/{_FIGURE}.jpg"
    await setup.objects.put(bad, b"x")
    assert (await setup.reconciler().run_once()).deleted == 1
    assert bad in await setup.objects.list_prefix("knowledge_figures/")


async def test_by_default_it_only_counts() -> None:
    # Re-review H1 (1): dry run unless the setting turns deleting on.
    setup = _Setup()
    _, gone = await setup.documents(kept=2, gone=1)
    reconciler = FigureReconciler(
        store=setup.store, storage=setup.storage, interval_s=60, now=_later
    )
    assert await reconciler.run_once() == ReconcileOutcome(orphans=1)
    assert str(gone[0]) in await setup.holders()


async def test_recent_originals_are_left_alone() -> None:
    # Re-review H1 (5): only originals older than a day.
    setup = _Setup()
    _, gone = await setup.documents(kept=2, gone=1)
    young = setup.reconciler(now=lambda: datetime.now(UTC) + timedelta(hours=23))
    assert await young.run_once() == ReconcileOutcome()
    assert str(gone[0]) in await setup.holders()


async def test_a_lookup_that_cannot_see_every_tenant_deletes_nothing() -> None:
    # Re-review H1 (4): row security still on for the connection.
    setup = _Setup(sees_all=False)
    await setup.documents(kept=2, gone=1)
    outcome = await setup.reconciler().run_once()
    assert outcome.aborted == "rls_not_bypassed" and outcome.deleted == 0
    assert len(await setup.holders()) == 3
    assert setup.store.checked_with_bypass == [True]
    assert setup.store.lookups == []


async def test_no_document_found_at_all_deletes_nothing() -> None:
    # Re-review H1 (3): originals in the bucket, none of their documents in
    # the database — the wrong database or an empty table, not "all deleted".
    setup = _Setup()
    await setup.documents(kept=0, gone=2)
    outcome = await setup.reconciler().run_once()
    assert outcome.aborted == "no_documents_found"
    assert len(await setup.holders()) == 2


async def test_most_documents_orphaned_deletes_nothing() -> None:
    # Re-review H1 (3): over half of 20+ documents gone means the lookup is off.
    setup = _Setup()
    await setup.documents(kept=9, gone=11)
    outcome = await setup.reconciler().run_once()
    assert outcome.aborted == "orphan_share"
    assert len(await setup.holders()) == 20


async def test_half_the_documents_orphaned_is_still_trusted() -> None:
    setup = _Setup()
    await setup.documents(kept=10, gone=10)
    outcome = await setup.reconciler().run_once()
    assert outcome == ReconcileOutcome(orphans=10, deleted=10)


async def test_a_pass_deletes_at_most_its_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(figure_reconcile, "MAX_DELETES_PER_PASS", 1)
    setup = _Setup()
    await setup.documents(kept=2, gone=2)
    assert await setup.reconciler().run_once() == ReconcileOutcome(orphans=2, deleted=1)
    assert len(await setup.holders()) == 3


async def test_a_store_without_modification_times_deletes_nothing() -> None:
    class _NoTimes(InMemoryObjectStore):
        list_prefix_modified = None  # type: ignore[assignment]

    setup = _Setup(objects=_NoTimes())
    await setup.documents(kept=2, gone=1)
    assert (await setup.reconciler().run_once()).aborted == "no_modification_times"
    assert len(await setup.holders()) == 3


async def test_the_lookup_spans_tenants_and_runs_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _Setup()
    await setup.documents(kept=1, gone=1)
    monkeypatch.setattr(figure_reconcile, "_LOOKUP_BATCH", 1)
    token = current_tenant_id_var.set(uuid4())
    try:
        await setup.reconciler().run_once()
    finally:
        current_tenant_id_var.reset(token)
    assert setup.store.lookups == [(True, None, 1), (True, None, 1)]
    assert bypass_rls_var.get() is False


async def test_nothing_stored_means_no_lookup() -> None:
    setup = _Setup()
    assert await setup.reconciler().run_once() == ReconcileOutcome()
    assert setup.store.lookups == [] and setup.store.checked_with_bypass == []


def test_it_runs_only_on_a_durable_store_and_the_sql_document_store() -> None:
    # Re-review H1 (2): "not found" from the in-memory store proves nothing.
    storage = FigureStorage(InMemoryObjectStore())
    sql = SqlKnowledgeStore(session_factory=None)  # type: ignore[arg-type]
    built = build_figure_reconciler(store=sql, storage=storage, interval_s=60, delete=False)
    assert isinstance(built, FigureReconciler)
    assert (
        build_figure_reconciler(
            store=InMemoryKnowledgeStore(), storage=storage, interval_s=60, delete=True
        )
        is None
    )
    assert build_figure_reconciler(store=sql, storage=None, interval_s=60, delete=True) is None


async def _passes_within(reconciler: FigureReconciler, seconds: float) -> int:
    passes: list[int] = []
    original = reconciler.run_once

    async def counting() -> ReconcileOutcome:
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("object store down")
        return await original()

    reconciler.run_once = counting  # type: ignore[method-assign]
    reconciler.start()
    await asyncio.sleep(seconds)
    await reconciler.stop()
    return len(passes)


async def test_the_first_pass_waits_after_start() -> None:
    # Re-review H1 (6): a fresh replica does not sweep at once.
    setup = _Setup()
    assert await _passes_within(setup.reconciler(initial_delay_s=30), 0.1) == 0


async def test_the_loop_runs_passes_and_survives_a_failing_one() -> None:
    setup = _Setup()
    await setup.documents(kept=2, gone=1)
    reconciler = setup.reconciler(interval_s=0.01, initial_delay_s=0)
    assert await _passes_within(reconciler, 0.3) >= 2
    assert len(await setup.holders()) == 2


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


async def test_under_twenty_documents_a_high_orphan_share_is_still_trusted() -> None:
    # Re-review round 4 (L7): below MIN_DOCUMENTS_FOR_SHARE only the
    # nothing-found guard applies.
    setup = _Setup()
    await setup.documents(kept=1, gone=2)
    assert await setup.reconciler().run_once() == ReconcileOutcome(orphans=2, deleted=2)


async def test_an_operator_can_let_a_mass_deletion_through() -> None:
    setup = _Setup()
    await setup.documents(kept=9, gone=11)
    outcome = await setup.reconciler(ignore_orphan_share=True).run_once()
    assert outcome == ReconcileOutcome(orphans=11, deleted=11)


async def test_a_lookup_failing_midway_deletes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-review round 4 (L7): a batch that failed proves nothing missing.
    monkeypatch.setattr(figure_reconcile, "_LOOKUP_BATCH", 1)
    setup = _Setup()
    await setup.documents(kept=2, gone=2)
    setup.store.fail_at = (3, ConnectionError("database blip"))
    assert (await setup.reconciler().run_once()).aborted == "lookup_failed"
    assert len(await setup.holders()) == 4


async def test_row_security_found_during_the_lookup_deletes_nothing() -> None:
    # Re-review round 4 (L6): the store's same-transaction check.
    setup = _Setup()
    await setup.documents(kept=2, gone=1)
    setup.store.fail_at = (1, RowSecurityActiveError("on"))
    assert (await setup.reconciler().run_once()).aborted == "rls_not_bypassed"
    assert len(await setup.holders()) == 3


class _CountingStorage(FigureStorage):
    """Spies on the listing pages and the per-document re-listing."""

    def __init__(self, objects: InMemoryObjectStore) -> None:
        super().__init__(objects)
        self.pages = 0
        self.relisted: list[UUID] = []

    async def key_pages(self) -> Any:
        async for page in self._store.iter_prefix_modified(  # type: ignore[attr-defined]
            "knowledge_figures/", page_size=2
        ):
            self.pages += 1
            yield page

    async def document_keys_modified(self, **kwargs: Any) -> Any:
        self.relisted.append(kwargs["document_id"])
        return await super().document_keys_modified(**kwargs)


async def test_the_bucket_is_read_page_by_page_and_orphans_listed_again() -> None:
    # Re-review round 4: one record per document from the scan; the keys to
    # delete come from listing each orphaned document again.
    setup = _Setup()
    kept, gone = await setup.documents(kept=3, gone=2)
    storage = _CountingStorage(setup.objects)
    reconciler = FigureReconciler(
        store=setup.store, storage=storage, interval_s=60, delete=True, now=_later
    )
    assert await reconciler.run_once() == ReconcileOutcome(orphans=2, deleted=2)
    assert storage.pages == 3  # 5 keys, 2 per page
    assert sorted(storage.relisted) == sorted(gone)
    assert await setup.holders() == {str(d) for d in kept}


async def test_only_one_replica_runs_a_pass() -> None:
    # Re-review round 4: a pg advisory lock makes the pass single-flight.
    setup = _Setup()
    await setup.documents(kept=2, gone=1)
    factory = FakeAdvisoryLockSessionFactory()
    holder = factory()
    await holder.execute(object(), {"cid": FIGURE_RECONCILE_LOCK_CLASSID, "k": "figure_reconcile"})
    blocked = setup.reconciler(session_factory=factory)
    assert await blocked.run_once() == ReconcileOutcome(skipped=True)
    assert setup.store.lookups == [] and len(await setup.holders()) == 3
    await holder.rollback()
    assert (await blocked.run_once()).deleted == 1


async def test_a_young_original_of_an_orphaned_document_stays() -> None:
    # The re-listing keeps the age rule: only keys older than a day go.
    setup = _Setup()
    _, (gone,) = await setup.documents(kept=2, gone=1)
    (old_key,) = [key for key in await setup.objects.list_prefix("") if str(gone) in key]
    young_key = old_key.replace("/g0/", "/g1/")
    await setup.objects.put(young_key, b"jpeg")
    record = setup.objects._objects[young_key]
    setup.objects._objects[young_key] = replace(record, modified_at=_later())
    assert await setup.reconciler().run_once() == ReconcileOutcome(orphans=1, deleted=1)
    remaining = await setup.objects.list_prefix("")
    assert young_key in remaining and old_key not in remaining


async def test_the_re_listing_stops_at_the_pass_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    # Round 5 (D): orphaned documents past the quota are not listed again.
    monkeypatch.setattr(figure_reconcile, "MAX_DELETES_PER_PASS", 1)
    setup = _Setup()
    await setup.documents(kept=3, gone=3)
    storage = _CountingStorage(setup.objects)
    reconciler = FigureReconciler(
        store=setup.store, storage=storage, interval_s=60, delete=True, now=_later
    )
    assert await reconciler.run_once() == ReconcileOutcome(orphans=3, deleted=1)
    assert len(storage.relisted) == 1


async def test_a_dry_run_counts_orphaned_documents_without_listing_them() -> None:
    setup = _Setup()
    await setup.documents(kept=3, gone=2)
    storage = _CountingStorage(setup.objects)
    reconciler = FigureReconciler(store=setup.store, storage=storage, interval_s=60, now=_later)
    assert await reconciler.run_once() == ReconcileOutcome(orphans=2)
    assert storage.relisted == []


async def test_the_share_override_warns_on_every_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Round 5 (H): an override left on does not go unnoticed.
    setup = _Setup()
    await setup.documents(kept=2, gone=1)
    reconciler = setup.reconciler(ignore_orphan_share=True, delete=False)
    with caplog.at_level("WARNING"):
        await reconciler.run_once()
        await reconciler.run_once()
    assert caplog.text.count("knowledge.figure_reconcile_orphan_share_override") == 2
    assert figure_reconcile._override._value.get() == 1
    await setup.reconciler().run_once()
    assert figure_reconcile._override._value.get() == 0


async def test_one_documents_many_originals_still_respect_the_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(figure_reconcile, "MAX_DELETES_PER_PASS", 1)
    setup = _Setup()
    _, (gone,) = await setup.documents(kept=2, gone=1)
    (key,) = [k for k in await setup.objects.list_prefix("") if str(gone) in k]
    await setup.objects.put(key.replace("/g0/", "/g1/"), b"jpeg")
    assert await setup.reconciler().run_once() == ReconcileOutcome(orphans=1, deleted=1)
    assert len([k for k in await setup.objects.list_prefix("") if str(gone) in k]) == 1
