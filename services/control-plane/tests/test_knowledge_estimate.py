"""B-222 PR-2 §4 —— the estimate of an ingest: counted locally, priced like the bill.

The estimate lists only what the tenant is billed (embeddings, scanned-page
recognition, figure descriptions), counts chunks as the ingest cuts them
(less the vectors the document already has from the same model), pages and
figures from the local parse (less what was read before when the bytes are
unchanged), takes per-call averages from the model catalog (else the platform's
generic ones, said so), prices with the rate card (no row: unpriced), refuses
what the ingest would refuse before anything is spent, and calls no model.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge import estimate as estimate_module
from control_plane.knowledge.chunking import (
    configure_semantic_breaks,
    count_tokens,
    semantic_breaks_enabled,
)
from control_plane.knowledge.estimate import DEFAULT_ESTIMATE_CAPS, IngestEstimator
from control_plane.knowledge.ingestion import (
    ingest_markdown,
    parse_locally,
    shutdown_parse_pool,
)
from control_plane.knowledge.parsed_size import (
    DocumentTooLargeError,
    DocumentTooManyChunksError,
    configure_parsed_size,
)
from control_plane.knowledge.parsing import DocumentParseError
from control_plane.knowledge.pricing import Rate, RateCardPrices, price_spend
from control_plane.knowledge.recognition.clients import Reading
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.ingest_parse import LocalParse
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.billing.rate_card import InMemoryModelRateCardStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DocumentPage,
    ModelRateCardUpsert,
    PageDecision,
    PageStatus,
    catalog_entry,
    knowledge_document_title,
)
from expert_work.protocol.knowledge_estimate import (
    EstimateBasis,
    EstimateKind,
    EstimateNote,
    IngestEstimate,
)
from expert_work.protocol.model_catalog import MODEL_CATALOG, EstimateCaps, ModelEntry
from tests.figure_test_images import noise_png
from tests.knowledge_form_fixtures import MIXED_DOCS, PICTURE_DOCS, picture_docx

_NOW = datetime(2026, 10, 28, tzinfo=UTC)
_EMBED = "acme/embed-1"
_TEXT = "\n\n".join(f"## Part {i}\n\nParagraph {i}: " + "word " * 120 for i in range(6))


@pytest.fixture(scope="module", autouse=True)
def _parse_pool() -> Iterator[None]:
    yield
    shutdown_parse_pool()


@pytest.fixture(autouse=True)
def _structural() -> Iterator[None]:
    """Topic shifts off unless a test turns them on (counts are simpler)."""
    previous = semantic_breaks_enabled()
    configure_semantic_breaks(enabled=False)
    yield
    configure_semantic_breaks(enabled=previous)


@dataclass
class _Prices:
    rates: dict[tuple[str, str], Rate] = field(default_factory=dict)
    asked: list[tuple[str, str]] = field(default_factory=list)

    async def rate(self, *, provider: str, model: str) -> Rate | None:
        self.asked.append((provider, model))
        return self.rates.get((provider, model))


class _ForbiddenModel:
    """Any model call fails the test: the estimate must call none."""

    async def read(self, image: bytes) -> Reading:
        raise AssertionError("a model was called")


@dataclass
class _Settings:
    primary: ModelChoice | None = field(default_factory=lambda: ModelChoice("acme", "ocr-1"))
    fallback: ModelChoice | None = field(default_factory=lambda: ModelChoice("acme", "vlm-1"))
    figure: ModelChoice | None = field(default_factory=lambda: ModelChoice("qwen", "qwen3.8-flash"))
    max_pages: int = 1000

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=self.primary,
            fallback=self.fallback,
            max_pages=self.max_pages,
            figure=self.figure,
        )


async def _key(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


async def _no_key(*, tenant_id: UUID, provider: str) -> str:
    raise LookupError(provider)


@dataclass
class _Setup:
    store: InMemoryKnowledgeStore
    tenant: UUID
    kb: UUID
    document: UUID
    filename: str
    raw: bytes
    estimator: IngestEstimator
    figures: FigureDescriber


def _setup(
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    kb: UUID,
    document: UUID,
    filename: str,
    raw: bytes,
    *,
    settings: _Settings | None = None,
    prices: _Prices | None = None,
    keys: Any = _key,
) -> _Setup:
    chosen = settings or _Settings()

    def reader(choice: ModelChoice, key: str) -> _ForbiddenModel:
        return _ForbiddenModel()

    def describer(
        choice: ModelChoice, key: str, prompt: str, user: str, cap: int
    ) -> _ForbiddenModel:
        return _ForbiddenModel()

    recognizer = DocumentRecognizer(
        store=store, settings=chosen, api_keys=keys, client_factory=reader
    )
    figures = FigureDescriber(
        store=store, settings=chosen, api_keys=keys, recognizer=recognizer, client_factory=describer
    )
    estimator = IngestEstimator(store=store, settings=chosen, recognizer=recognizer, prices=prices)
    return _Setup(store, tenant, kb, document, filename, raw, estimator, figures)


async def _new(
    filename: str,
    raw: bytes,
    *,
    settings: _Settings | None = None,
    prices: _Prices | None = None,
    keys: Any = _key,
) -> _Setup:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=filename, content=raw, content_sha256="sha-1"
    )
    return _setup(
        store,
        tenant,
        base.id,
        document.id,
        filename,
        raw,
        settings=settings,
        prices=prices,
        keys=keys,
    )


def _claim(setup: _Setup, *, serving: str | None = None, sha: str = "sha-1") -> ClaimedIngestion:
    return ClaimedIngestion(
        tenant_id=setup.tenant,
        document_id=setup.document,
        kb_id=setup.kb,
        filename=setup.filename,
        content=setup.raw,
        content_sha256=sha,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
        phase="estimate",
        origin="upload",
        serving_sha256=serving,
    )


async def _local(setup: _Setup) -> LocalParse:
    return await parse_locally(
        setup.store,
        tenant_id=setup.tenant,
        kb_id=setup.kb,
        document_id=setup.document,
        filename=setup.filename,
        raw=setup.raw,
        figures=setup.figures,
    )


async def _estimate(
    setup: _Setup, local: LocalParse | None = None, *, model: str | None = _EMBED, **claim: Any
) -> IngestEstimate:
    return await setup.estimator.estimate(
        _claim(setup, **claim),
        local or await _local(setup),
        embedding_model=model,
        now=_NOW,
    )


def _items(estimate: IngestEstimate, kind: EstimateKind) -> list[Any]:
    return [item for item in estimate.items if item.kind is kind]


def _text_local(text: str = _TEXT) -> LocalParse:
    return LocalParse(kind="text", figures_on=False, markdown=text)


def _chunk_tokens(text: str) -> int:
    """What the ingest embeds for ``text`` (each chunk's retrieval text)."""
    from control_plane.knowledge.chunking import chunk_markdown_parented
    from expert_work.protocol import chunk_retrieval_text

    title = knowledge_document_title("guide.md")
    parents = chunk_markdown_parented(
        text, max_tokens=DEFAULT_CHUNK_MAX_TOKENS, overlap_tokens=0, doc_title=title
    )
    texts = {
        chunk_retrieval_text(c.heading_path, c.body, title)
        for parent in parents
        for c in parent.children
    }
    return sum(count_tokens(t) for t in texts)


# -- embeddings ---------------------------------------------------------------------


async def test_only_what_the_tenant_pays_for_is_listed_and_it_is_priced_like_the_bill() -> None:
    prices = _Prices({("acme", "embed-1"): Rate(500_000, 0)})
    setup = await _new("guide.md", _TEXT.encode(), prices=prices)
    estimate = await _estimate(setup, _text_local())
    assert {item.kind for item in estimate.items} == {EstimateKind.EMBEDDING}
    (line,) = estimate.items
    tokens = _chunk_tokens(_TEXT)
    assert line.input_tokens == tokens  # coefficient 1.0 by default
    assert line.micros == tokens * 500_000 // 1_000_000
    assert estimate.total_micros == line.micros
    assert line.basis is EstimateBasis.DEFAULT_AVERAGE
    assert EstimateNote.DEFAULT_AVERAGE.value in estimate.notes
    assert not estimate.unpriced
    assert estimate.models.embedding == _EMBED
    assert estimate.models_fingerprint == estimate.models.fingerprint()
    assert estimate.counts.chunks == line.units > 0


async def test_vectors_the_document_already_has_from_that_model_are_not_counted() -> None:
    setup = await _new("guide.md", _TEXT.encode())

    class _Embedder:
        model_key = _EMBED

        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            return [(1.0,) + (0.0,) * 1023 for _ in texts]

    await ingest_markdown(
        store=setup.store,
        embedder=_Embedder(),
        tenant_id=setup.tenant,
        document_id=setup.document,
        kb_id=setup.kb,
        markdown=_TEXT,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=0,
        doc_title=knowledge_document_title("guide.md"),
    )
    same = await _estimate(setup, _text_local())
    (line,) = same.items
    assert line.reused_units == line.units
    assert line.input_tokens == 0
    # Another model: nothing to reuse.
    other = await _estimate(setup, _text_local(), model="acme/embed-2")
    assert other.items[0].reused_units == 0
    assert other.items[0].input_tokens == _chunk_tokens(_TEXT)


async def test_topic_detection_is_counted_too() -> None:
    plain = "\n\n".join(f"Paragraph {i}: " + "word " * 120 for i in range(6))
    setup = await _new("guide.md", plain.encode())
    structural = (await _estimate(setup, _text_local(plain))).items[0].input_tokens
    configure_semantic_breaks(enabled=True)
    with_shifts = (await _estimate(setup, _text_local(plain))).items[0].input_tokens
    assert with_shifts > structural


async def test_no_rate_card_row_means_unpriced_not_free() -> None:
    setup = await _new("guide.md", _TEXT.encode(), prices=_Prices())
    estimate = await _estimate(setup, _text_local())
    assert estimate.total_micros == 0
    assert estimate.unpriced
    assert not estimate.items[0].priced
    assert EstimateNote.UNPRICED.value in estimate.notes


async def test_the_rate_card_prices_it() -> None:
    cards = InMemoryModelRateCardStore()
    await cards.create(
        upsert=ModelRateCardUpsert(
            provider="qwen",
            model="text-embedding-v4",
            input_per_mtok_micros=500_000,
            output_per_mtok_micros=0,
        ),
        actor_id="admin",
    )
    setup = await _new("guide.md", _TEXT.encode())
    setup.estimator._prices = RateCardPrices(cards)
    estimate = await _estimate(setup, _text_local(), model="qwen/text-embedding-v4")
    line = estimate.items[0]
    assert line.priced and line.micros == line.input_tokens * 500_000 // 1_000_000


async def test_averages_come_from_each_models_own_catalog_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = {
        ("acme", "embed-1"): EstimateCaps(embed_tokens_per_local_token=1.5),
        ("other", "embed-9"): EstimateCaps(embed_tokens_per_local_token=0.5),
    }

    def entry(provider: str, model: str) -> ModelEntry | None:
        found = caps.get((provider, model))
        return ModelEntry(name=model, embeddings=True, estimate_caps=found) if found else None

    monkeypatch.setattr(estimate_module, "catalog_entry", entry)
    setup = await _new("guide.md", _TEXT.encode())
    tokens = _chunk_tokens(_TEXT)
    acme = (await _estimate(setup, _text_local())).items[0]
    other = (await _estimate(setup, _text_local(), model="other/embed-9")).items[0]
    unknown = await _estimate(setup, _text_local(), model="nobody/embed")
    assert acme.input_tokens == math.ceil(tokens * 1.5)
    assert acme.basis is EstimateBasis.LOCAL_COUNT
    assert other.input_tokens == math.ceil(tokens * 0.5)
    assert unknown.items[0].input_tokens == tokens
    assert unknown.items[0].basis is EstimateBasis.DEFAULT_AVERAGE


async def test_what_an_ingest_cost_is_priced_row_by_row_like_the_bill() -> None:
    prices = _Prices({("acme", "ocr-1"): Rate(600_000, 0)})
    actual = await price_spend(
        prices,
        # Each row is 0.6 micro-yuan: the bill floors every row to 0.
        [("acme", "ocr-1", 1, 0), ("acme", "ocr-1", 1, 0), (None, "nobody-model", 5, 5)],
        estimate_id=None,
        generation=3,
        outcome="ready",
        now=_NOW,
    )
    priced, unpriced = actual.items
    assert (priced.model, priced.input_tokens, priced.micros, priced.priced) == (
        "ocr-1",
        2,
        0,
        True,
    )
    assert (unpriced.model, unpriced.priced, unpriced.micros) == ("nobody-model", False, 0)
    assert actual.total_micros == 0
    assert actual.unpriced
    assert actual.generation == 3


async def test_what_the_ingest_would_refuse_is_refused_before_anything_is_spent() -> None:
    setup = await _new("guide.md", _TEXT.encode())
    configure_parsed_size(max_chars=100, max_chunks=10_000)
    try:
        with pytest.raises(DocumentTooLargeError):
            await _estimate(setup, _text_local())
        configure_parsed_size(max_chars=2_500_000, max_chunks=2)
        with pytest.raises(DocumentTooManyChunksError):
            await _estimate(setup, _text_local())
    finally:
        configure_parsed_size(max_chars=2_500_000, max_chunks=60_000)


# -- scanned pages --------------------------------------------------------------------

_MIXED = MIXED_DOCS[0]  # text page, scanned page, text page


async def test_scanned_pages_are_counted_and_read_by_the_configured_models() -> None:
    prices = _Prices(
        {
            ("acme", "ocr-1"): Rate(200_000, 200_000),
            ("acme", "vlm-1"): Rate(800_000, 800_000),
            ("acme", "embed-1"): Rate(500_000, 0),
        }
    )
    setup = await _new(_MIXED.filename, _MIXED.raw, prices=prices)
    estimate = await _estimate(setup)
    primary, fallback = _items(estimate, EstimateKind.RECOGNITION)
    assert (primary.model, primary.units, primary.reused_units) == ("acme/ocr-1", 1, 0)
    assert primary.input_tokens == DEFAULT_ESTIMATE_CAPS.page_input_tokens
    assert primary.output_tokens == DEFAULT_ESTIMATE_CAPS.page_output_tokens
    assert primary.micros == (2_733 * 200_000 + 8_192 * 200_000) // 1_000_000
    # The fallback reads 10 % of them again, rounded up.
    assert (fallback.model, fallback.units) == ("acme/vlm-1", 1)
    assert estimate.counts.scanned_pages == 1
    assert estimate.models.recognition == "acme/ocr-1"
    assert estimate.models.fallback == "acme/vlm-1"
    # What recognition reads is embedded too.
    (embedding,) = _items(estimate, EstimateKind.EMBEDDING)
    assert embedding.input_tokens >= DEFAULT_ESTIMATE_CAPS.page_output_tokens
    assert EstimateNote.TEXT_LIMIT_MAY_STOP_EARLY.value in estimate.notes


async def test_pages_past_the_limit_are_not_counted() -> None:
    setup = await _new(_MIXED.filename, _MIXED.raw, settings=_Settings(max_pages=0))
    estimate = await _estimate(setup)
    (line,) = _items(estimate, EstimateKind.RECOGNITION)
    assert line.units == 0 and line.input_tokens == 0
    assert estimate.counts.pages_over_limit == 1
    assert EstimateNote.OVER_PAGE_LIMIT.value in estimate.notes
    # Nothing is read: the recognition models do not change the estimate.
    assert estimate.models.recognition is None


async def test_without_a_usable_recognition_model_the_pages_wait_unbilled() -> None:
    setup = await _new(_MIXED.filename, _MIXED.raw, keys=_no_key)
    estimate = await _estimate(setup)
    (line,) = _items(estimate, EstimateKind.RECOGNITION)
    assert (line.units, line.input_tokens, line.output_tokens, line.micros) == (1, 0, 0, 0)
    assert EstimateNote.RECOGNITION_UNAVAILABLE.value in estimate.notes


async def test_pages_read_before_are_not_counted_again_when_the_bytes_are_unchanged() -> None:
    setup = await _new(_MIXED.filename, _MIXED.raw)
    await setup.store.set_document_pages(
        tenant_id=setup.tenant,
        document_id=setup.document,
        pages=[
            DocumentPage(page=1, decision=PageDecision.TEXT),
            DocumentPage(page=2, decision=PageDecision.OCR, status=PageStatus.RECOGNIZED),
            DocumentPage(page=3, decision=PageDecision.TEXT),
        ],
    )
    same = await _estimate(setup, serving="sha-1")
    (line,) = _items(same, EstimateKind.RECOGNITION)
    assert (line.units, line.reused_units, line.input_tokens) == (1, 1, 0)
    changed = await _estimate(setup, serving="sha-0")
    assert _items(changed, EstimateKind.RECOGNITION)[0].reused_units == 0


# -- figures ---------------------------------------------------------------------------

_PICTURE = PICTURE_DOCS[0]  # a Word file that is one describable picture


async def test_figures_are_counted_from_the_local_parse() -> None:
    prices = _Prices({("qwen", "qwen3.8-flash"): Rate(800_000, 2_700_000)})
    setup = await _new(_PICTURE.filename, _PICTURE.raw, prices=prices)
    estimate = await _estimate(setup)
    (line,) = _items(estimate, EstimateKind.FIGURE)
    assert (line.model, line.units, line.reused_units) == ("qwen/qwen3.8-flash", 1, 0)
    # The model's own catalog averages (B-222 calibration), not the generic ones.
    caps = catalog_entry("qwen", "qwen3.8-flash").estimate_caps  # type: ignore[union-attr]
    assert caps is not None
    assert (line.input_tokens, line.output_tokens) == (
        caps.figure_input_tokens,
        caps.figure_output_tokens,
    )
    assert line.basis is EstimateBasis.CATALOG_AVERAGE
    assert line.micros == (2_711 * 800_000 + 1_024 * 2_700_000) // 1_000_000
    assert estimate.counts.figures == 1
    assert estimate.models.figure == "qwen/qwen3.8-flash"
    assert EstimateNote.BATCH_REPEATS_COUNTED.value in estimate.notes
    # One call a figure: charts / text-heavy figures may cost more, and it says so.
    assert EstimateNote.FIGURE_EXTRA_CALLS_NOT_COUNTED.value in estimate.notes


async def test_a_figure_repeated_across_the_tenant_is_not_counted() -> None:
    setup = await _new(_PICTURE.filename, _PICTURE.raw)
    local = await _local(setup)
    assert local.office_figures is not None
    pixel_hash = local.office_figures.candidates[0].pixel_hash
    for _ in range(2):  # two other documents of the tenant hold it: three with this one
        other = await setup.store.upsert_document(
            tenant_id=setup.tenant, kb_id=setup.kb, filename=f"{uuid4()}.docx", content=b"x"
        )
        await setup.store.set_document_figures(
            tenant_id=setup.tenant, document_id=other.id, stats=None, pixel_hashes=[pixel_hash]
        )
    estimate = await _estimate(setup, local)
    assert _items(estimate, EstimateKind.FIGURE) == []
    assert estimate.models.figure is None


async def test_with_figure_description_off_the_pictures_are_left_out_and_said_so() -> None:
    raw = picture_docx(noise_png(900, 700, seed=5), before="Device manual.", after="The end.")
    setup = await _new("manual.docx", raw, settings=_Settings(figure=None))
    estimate = await _estimate(setup)
    assert _items(estimate, EstimateKind.FIGURE) == []
    assert EstimateNote.FIGURES_OFF.value in estimate.notes


async def test_figures_described_before_are_not_counted_again() -> None:
    from expert_work.protocol import FigureStats

    setup = await _new(_PICTURE.filename, _PICTURE.raw)
    await setup.store.set_document_figures(
        tenant_id=setup.tenant,
        document_id=setup.document,
        stats=FigureStats(extracted=1, described=1),
        pixel_hashes=[],
    )
    same = await _estimate(setup, serving="sha-1")
    (line,) = _items(same, EstimateKind.FIGURE)
    assert (line.reused_units, line.input_tokens) == (1, 0)


async def test_a_picture_only_file_without_figure_description_fails_for_free() -> None:
    # Nothing would describe it: parsing finds no text — refused before any model.
    setup = await _new(_PICTURE.filename, _PICTURE.raw, settings=_Settings(figure=None))
    with pytest.raises(DocumentParseError):
        await _local(setup)


def test_the_generic_averages_err_on_the_high_side() -> None:
    # The highest the calibrated models take: an unmeasured model is never
    # estimated below them (the user confirms a number, then pays).
    assert DEFAULT_ESTIMATE_CAPS == EstimateCaps(
        page_input_tokens=2_733,
        page_output_tokens=8_192,
        figure_input_tokens=2_711,
        figure_output_tokens=1_024,
        embed_tokens_per_local_token=1.0,
    )
    declared = [entry.estimate_caps for entries in MODEL_CATALOG.values() for entry in entries]
    measured = [caps for caps in declared if caps is not None]
    assert measured
    for field_name in EstimateCaps.model_fields:
        values = [getattr(caps, field_name) for caps in measured]
        default = getattr(DEFAULT_ESTIMATE_CAPS, field_name)
        assert all(value <= default for value in values if value is not None), field_name
