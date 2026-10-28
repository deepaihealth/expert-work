"""B-222 —— cancelling an ingest: the edges.

* ``indexing`` is the point of no return: once the new chunks are being
  swapped in, a cancel is refused (``not_cancellable``) and the ingest
  finishes, so a cancelled status never sits on top of a new version's chunks.
* A re-index re-embeds every document whose previous version still serves
  (ready, or a re-upload that failed / was cancelled).
* A checkpoint that errors (a database blip) is not a cancel.
* ``cancel_local`` stops the ingest on this pod whichever worker drives it,
  and a stale task of the same document ending does not unregister the newer one.

Embedding models are fakes (no network); texts are synthesised here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    ingest_markdown,
    shutdown_parse_pool,
)
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.recognizer import RecognitionSettings
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import CancelOutcome, ClaimedIngestion
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DocumentStatus,
)
from orchestrator.llm import FakeEmbedder

_TENANT = uuid4()
_V1 = b"# Old\n\nThe old deductible is 500 yuan per claim."
_V2 = b"# New\n\nThe new deductible is 900 yuan per claim."


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


class _CancelAfterSwap(InMemoryKnowledgeStore):
    """A cancel request that lands right after the chunks were swapped in,
    before the READY write (what an SQL cancel blocked on the swap's row lock
    does). Armed by setting ``kb_id``; records each outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.kb_id: UUID | None = None
        self.outcomes: list[CancelOutcome] = []

    async def replace_chunks(self, **kwargs: Any) -> bool:
        written = await super().replace_chunks(**kwargs)
        if written and self.kb_id is not None:
            document_id = kwargs["document_id"]
            outcome = await self.cancel_documents(
                tenant_id=kwargs["tenant_id"], kb_id=self.kb_id, document_ids=[document_id]
            )
            self.outcomes.append(outcome[document_id])
        return written


# ---------------------------------------------------------------------------
# indexing is the point of no return
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_cancel_after_the_swap_is_refused_and_the_new_version_settles() -> None:
    store = _CancelAfterSwap()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V1, content_sha256="v1"
    )
    await runner.submit(tenant_id=_TENANT, document_id=document.id)
    await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V2, content_sha256="v2"
    )
    store.kb_id = base.id

    await runner.submit(tenant_id=_TENANT, document_id=document.id)

    assert store.outcomes == ["not_cancellable"]
    settled = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert settled is not None
    assert settled.status is DocumentStatus.READY
    assert not settled.has_pending
    chunks, total = await store.list_chunks(tenant_id=_TENANT, document_id=document.id)
    assert settled.chunk_count == total
    assert all("900" in chunk.content for chunk in chunks)
    assert await store.get_document_content(tenant_id=_TENANT, document_id=document.id) == _V2


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_an_empty_new_version_reaches_indexing_before_it_clears_the_chunks() -> None:
    """A version with no text replaces the chunks with none: that swap is past
    the point of no return too, or a cancel would leave the document with its
    serving chunks gone."""
    store = _CancelAfterSwap()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=b" ", content_sha256="empty"
    )
    claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None
    store.kb_id = base.id

    count = await ingest_markdown(
        store=store,
        embedder=FakeEmbedder(),
        tenant_id=_TENANT,
        document_id=document.id,
        kb_id=base.id,
        markdown=" ",
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        generation=claim.generation,
    )

    assert count == 0
    assert store.outcomes == ["not_cancellable"]


# ---------------------------------------------------------------------------
# re-index: a previous version still serving is re-embedded too
# ---------------------------------------------------------------------------


class _Pinned(FakeEmbedder):
    """A fixed embedding model (``model_key``): the chunks record it."""

    def __init__(self, model_key: str) -> None:
        super().__init__()
        self.model_key = model_key


async def _models(store: InMemoryKnowledgeStore, document_id: UUID) -> set[str | None]:
    chunks, _ = await store.list_chunks(tenant_id=_TENANT, document_id=document_id)
    return {chunk.embedding_model for chunk in chunks}


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_reindex_re_embeds_previous_versions_still_serving() -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    old = KnowledgeIngestionRunner(store=store, embedder=_Pinned("prov/old"))
    ids: dict[str, UUID] = {}
    for name in ("ready.md", "cancelled.md", "failed.md", "never.md"):
        document = await store.upsert_document(
            tenant_id=_TENANT, kb_id=base.id, filename=name, content=_V1, content_sha256=name
        )
        ids[name] = document.id
        if name != "never.md":
            await old.submit(tenant_id=_TENANT, document_id=document.id)
    # Re-uploads that were cancelled / failed: the previous version serves on.
    for name in ("cancelled.md", "failed.md"):
        await store.upsert_document(
            tenant_id=_TENANT, kb_id=base.id, filename=name, content=_V2, content_sha256="v2"
        )
    claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=ids["failed.md"],
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None
    assert await store.mark_document_failed_terminal(
        tenant_id=_TENANT, document_id=ids["failed.md"], error="boom", generation=claim.generation
    )
    # Cancelled before its first ingest: nothing serves, nothing to re-embed.
    outcomes = await store.cancel_documents(
        tenant_id=_TENANT, kb_id=base.id, document_ids=[ids["cancelled.md"], ids["never.md"]]
    )
    assert set(outcomes.values()) == {"cancelled"}

    new = KnowledgeIngestionRunner(store=store, embedder=_Pinned("prov/new"))
    await new.submit_reindex(
        tenant_id=_TENANT, kb_id=base.id, embedding_provider="prov", embedding_model="new"
    )

    for name in ("ready.md", "cancelled.md", "failed.md"):
        assert await _models(store, ids[name]) == {"prov/new"}, name
    assert await _models(store, ids["never.md"]) == set()
    statuses = {
        name: document.status
        for name, document in (
            await store.get_documents(tenant_id=_TENANT, document_ids=list(ids.values()))
        ).items()
    }
    assert statuses == {
        ids["ready.md"]: DocumentStatus.READY,
        ids["cancelled.md"]: DocumentStatus.CANCELLED,
        ids["failed.md"]: DocumentStatus.FAILED,
        ids["never.md"]: DocumentStatus.CANCELLED,
    }
    [stamped] = await store.list_bases(tenant_id=_TENANT)
    assert (stamped.embedding_provider, stamped.embedding_model) == ("prov", "new")


async def _base_model(store: InMemoryKnowledgeStore) -> tuple[str | None, str | None]:
    [base] = await store.list_bases(tenant_id=_TENANT)
    return base.embedding_provider, base.embedding_model


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_reindex_stays_unstamped_while_a_serving_document_is_re_uploading() -> None:
    """A re-upload queued while the re-index runs is not re-embedded, and its
    previous version still serves (cancelling the re-upload keeps it): the
    base must keep saying it needs a re-index."""
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    old = KnowledgeIngestionRunner(store=store, embedder=_Pinned("prov/old"))
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="y.md", content=_V1, content_sha256="v1"
    )
    await old.submit(tenant_id=_TENANT, document_id=document.id)
    await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="y.md", content=_V2, content_sha256="v2"
    )
    new = KnowledgeIngestionRunner(store=store, embedder=_Pinned("prov/new"))

    await new.submit_reindex(
        tenant_id=_TENANT, kb_id=base.id, embedding_provider="prov", embedding_model="new"
    )

    assert await _base_model(store) == (None, None)  # unstamped: needs_reindex stays true
    await _cancel(store, base.id, document.id)
    await new.submit_reindex(
        tenant_id=_TENANT, kb_id=base.id, embedding_provider="prov", embedding_model="new"
    )
    assert await _models(store, document.id) == {"prov/new"}
    assert await _base_model(store) == ("prov", "new")


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_reindex_re_embeds_chunks_of_a_first_upload_that_failed_after_its_swap() -> None:
    """Chunks written, then READY refused (failed): ``chunk_count`` was never
    set, but the chunks serve — they are re-embedded too."""
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V1, content_sha256="v1"
    )
    claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None
    await ingest_markdown(
        store=store,
        embedder=_Pinned("prov/old"),
        tenant_id=_TENANT,
        document_id=document.id,
        kb_id=base.id,
        markdown=_V1.decode(),
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        generation=claim.generation,
    )
    assert await store.mark_document_failed_terminal(
        tenant_id=_TENANT, document_id=document.id, error="boom", generation=claim.generation
    )
    failed = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert failed is not None and failed.chunk_count == 0

    new = KnowledgeIngestionRunner(store=store, embedder=_Pinned("prov/new"))
    await new.submit_reindex(
        tenant_id=_TENANT, kb_id=base.id, embedding_provider="prov", embedding_model="new"
    )

    assert await _models(store, document.id) == {"prov/new"}


# ---------------------------------------------------------------------------
# a checkpoint that errors is not a cancel
# ---------------------------------------------------------------------------


class _FlakyRenewStore(InMemoryKnowledgeStore):
    """``renew_lease`` fails once (a dropped database connection), on its 2nd call."""

    def __init__(self) -> None:
        super().__init__()
        self.renewals = 0

    async def renew_lease(self, **kwargs: Any) -> bool:
        self.renewals += 1
        if self.renewals == 2:
            raise ConnectionError("server closed the connection unexpectedly")
        return await super().renew_lease(**kwargs)


def _entries(count: int) -> str:
    """Headed entries, each long enough to stand as a chunk of its own."""
    return "\n\n".join(
        f"## Entry {i}\n\n" + f"Entry {i} explains one rule of the handbook. " * 8
        for i in range(count)
    )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_checkpoint_that_errors_does_not_fail_the_document() -> None:
    store = _FlakyRenewStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT,
        kb_id=base.id,
        filename="entries.md",
        content=_entries(450).encode(),
        content_sha256="entries",
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())

    await runner.submit(tenant_id=_TENANT, document_id=document.id)

    # Three embedding windows, three checkpoints: the second one errored.
    assert store.renewals >= 3
    settled = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert settled is not None
    assert (settled.status, settled.chunk_count) == (DocumentStatus.READY, 450)


# ---------------------------------------------------------------------------
# stopping at once on this pod: whichever worker runs the ingest
# ---------------------------------------------------------------------------


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class _GatedEmbedder:
    """Call ``i`` of ``embed`` blocks until ``gates[i]`` opens (an embedding
    request in flight); records whether a call was aborted."""

    def __init__(self, calls: int) -> None:
        self.entered = [asyncio.Event() for _ in range(calls)]
        self.gates = [asyncio.Event() for _ in range(calls)]
        self.aborted = False
        self._calls = 0

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        call = self._calls
        self._calls += 1
        self.entered[call].set()
        try:
            await self.gates[call].wait()
        except asyncio.CancelledError:
            self.aborted = True
            raise
        return await FakeEmbedder().embed(texts, tenant_id=tenant_id)


async def _cancel(store: InMemoryKnowledgeStore, kb_id: UUID, document_id: UUID) -> None:
    outcome = await store.cancel_documents(
        tenant_id=_TENANT, kb_id=kb_id, document_ids=[document_id]
    )
    assert outcome == {document_id: "cancelled"}


async def test_the_lifespan_gives_runner_and_recovery_worker_one_registry() -> None:
    """``cancel_local`` (through the runner) must reach the recovery worker's
    drives: the app wires both to the same ``LocalIngests``."""
    from control_plane import app as app_module
    from control_plane.settings import Settings
    from tests.auth_fixtures import build_test_jwt_verifier

    app = app_module.create_app(
        settings=Settings(checkpointer_backend="memory", enable_knowledge_recovery_worker=True),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        runner = app.state.knowledge_ingestion_runner
        worker = app.state.knowledge_recovery_worker
        assert isinstance(runner, KnowledgeIngestionRunner)
        assert isinstance(worker, KnowledgeIngestRecoveryWorker)
        assert worker.local is runner.local


async def test_a_stale_task_ending_leaves_the_newer_claim_cancellable() -> None:
    """Task A ingests v1; a re-upload's task B claims v2 of the same document.
    A ending (stale) must not unregister B: cancelling must still stop B."""
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    embedder = _GatedEmbedder(calls=2)
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V1, content_sha256="v1"
    )
    try:
        stale = runner.submit(tenant_id=_TENANT, document_id=document.id)
        await asyncio.wait_for(embedder.entered[0].wait(), timeout=30)
        await store.upsert_document(
            tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V2, content_sha256="v2"
        )
        fresh = runner.submit(tenant_id=_TENANT, document_id=document.id)
        await asyncio.wait_for(embedder.entered[1].wait(), timeout=30)
        embedder.gates[0].set()
        await asyncio.wait_for(stale, timeout=5)  # its fenced writes are refused

        await _cancel(store, base.id, document.id)
        assert runner.cancel_local([document.id]) == 1
        await asyncio.wait_for(fresh, timeout=5)
    finally:
        for gate in embedder.gates:
            gate.set()

    assert embedder.aborted
    cancelled = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert cancelled is not None and cancelled.status is DocumentStatus.CANCELLED


async def test_cancel_local_stops_a_recovery_drive_too(caplog: pytest.LogCaptureFixture) -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V1, content_sha256="v1"
    )
    embedder = _GatedEmbedder(calls=1)
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=embedder,
        interval_s=30,
        concurrency=1,
        lease_seconds=300,
        max_attempts=5,
        local=runner.local,
    )
    try:
        sweep = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(embedder.entered[0].wait(), timeout=30)

        await _cancel(store, base.id, document.id)
        assert runner.cancel_local([document.id]) == 1
        settled = await asyncio.wait_for(sweep, timeout=5)
    finally:
        embedder.gates[0].set()

    assert settled == 0
    assert embedder.aborted
    assert "drive_crashed" not in caplog.text
    cancelled = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert cancelled is not None and cancelled.status is DocumentStatus.CANCELLED
    assert runner.cancel_local([document.id]) == 0  # the drive unregistered itself


class _FailureSpyStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 0

    async def mark_document_failed_terminal(self, **kwargs: Any) -> bool:
        self.failures += 1
        return await super().mark_document_failed_terminal(**kwargs)


def _recovery(
    store: InMemoryKnowledgeStore, embedder: Any, **kwargs: Any
) -> KnowledgeIngestRecoveryWorker:
    """A recovery worker renewing its lease every 0.1 s."""
    return KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=embedder,
        interval_s=30,
        concurrency=1,
        lease_seconds=0.3,  # type: ignore[arg-type]
        max_attempts=5,
        **kwargs,
    )


async def test_a_recovery_drive_stops_when_its_lease_renewal_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelled from another pod while the drive is inside a call with no
    checkpoint: the next lease renewal is refused and stops it."""
    store = _FailureSpyStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V1, content_sha256="v1"
    )
    embedder = _GatedEmbedder(calls=1)
    try:
        sweep = asyncio.create_task(_recovery(store, embedder).run_once())
        await asyncio.wait_for(embedder.entered[0].wait(), timeout=30)

        await _cancel(store, base.id, document.id)  # no cancel_local: another pod
        settled = await asyncio.wait_for(sweep, timeout=5)
    finally:
        embedder.gates[0].set()

    assert settled == 0
    assert embedder.aborted
    assert store.failures == 0
    assert "drive_crashed" not in caplog.text


class _NoRecognitionSettings:
    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(primary=None, fallback=None, max_pages=1000)


async def _no_keys(*, tenant_id: UUID, provider: str) -> str:
    raise LookupError(provider)


class _SlowTidy(FigureDescriber):
    """No figures to describe; tidying up after the ingest takes a while."""

    def __init__(self, store: InMemoryKnowledgeStore) -> None:
        super().__init__(store=store, settings=_NoRecognitionSettings(), api_keys=_no_keys)
        self.tidied = False
        self.settled: list[bool] = []

    async def enabled(self, *, tenant_id: UUID) -> bool:
        del tenant_id
        return False

    async def tidy(self, claim: ClaimedIngestion, *, settled: bool, serving: bool = False) -> None:
        del claim, serving
        self.settled.append(settled)
        await asyncio.sleep(0.5)  # several renewal intervals
        self.tidied = True


async def test_a_renewal_refused_after_ready_does_not_cut_the_tidy_short() -> None:
    """Once the document is READY its renewals are refused — that is the drive
    finishing, not losing the document: its tidy-up runs to the end."""
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V1, content_sha256="v1"
    )
    figures = _SlowTidy(store)

    settled = await _recovery(store, FakeEmbedder(), figures=figures).run_once()

    assert settled == 1
    assert figures.tidied
    ready = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert ready is not None and ready.status is DocumentStatus.READY


class _SlowReadyAck(InMemoryKnowledgeStore):
    """The READY write lands, then its acknowledgement takes a while (a slow
    commit round trip): renewals made meanwhile are refused (status ready)."""

    async def set_document_status(self, **kwargs: Any) -> bool:
        written = await super().set_document_status(**kwargs)
        if kwargs["status"] is DocumentStatus.READY:
            await asyncio.sleep(0.35)  # several renewal intervals
        return written


async def test_a_renewal_refused_while_ready_is_written_does_not_stop_the_fast_path() -> None:
    """The upload runner too: once the paid work is over, a refused renewal is
    the document settling — the ingest finishes as READY and tidies as such."""
    store = _SlowReadyAck()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_V1, content_sha256="v1"
    )
    figures = _SlowTidy(store)
    runner = KnowledgeIngestionRunner(
        store=store,
        embedder=FakeEmbedder(),
        lease_seconds=0.3,  # type: ignore[arg-type]  # renewed every 0.1 s
        figures=figures,
    )

    await runner.submit(tenant_id=_TENANT, document_id=document.id)

    assert figures.settled == [True]
    assert figures.tidied
    ready = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert ready is not None and ready.status is DocumentStatus.READY
