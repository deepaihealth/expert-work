"""B-222 PR-2 §4 —— the estimate of an ingest, before anything is spent.

Counts come from the local parse (:mod:`~control_plane.knowledge.estimate_counts`);
what a page, a figure or an embedded token takes on a model comes from the
model catalog (``ModelEntry.estimate_caps``), else from the platform's generic
averages below — the line then says so (``default_average``, 「按通用均值估」);
prices from the rate card (:mod:`~control_plane.knowledge.pricing`). Only what
is billed to the tenant is listed: embeddings, scanned-page recognition, figure
descriptions. Nothing here calls a model or names a vendor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final
from uuid import uuid4

from control_plane.knowledge.chunking import semantic_breaks_enabled
from control_plane.knowledge.estimate_counts import (
    EmbeddingCount,
    FigureCount,
    PageCount,
    count_embedding,
    count_figures,
    count_pages,
    embedding_text,
)
from control_plane.knowledge.parsing import FIGURE_OFFICE_EXTENSIONS, is_plain_text_file
from control_plane.knowledge.pricing import PriceSource, price_tokens, split_model_key
from control_plane.knowledge.recognition.ingest_parse import LocalParse
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    RecognitionSettingsSource,
)
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.protocol import catalog_entry, knowledge_document_title
from expert_work.protocol.knowledge_estimate import (
    EstimateBasis,
    EstimateCounts,
    EstimateItem,
    EstimateKind,
    EstimateModels,
    EstimateNote,
    EstimateOrigin,
    EstimateUnit,
    IngestEstimate,
)
from expert_work.protocol.model_catalog import EstimateCaps

#: The platform's generic averages (「按通用均值估」), for a model whose catalog
#: entry declares none — the highest the calibrated models take (a user who
#: confirmed a number must not pay more): a page as the 2,000 px render reads
#: and the platform's recognition output cap; a figure as the largest measured
#: and the description output cap. Embedding tokens: one per local token.
DEFAULT_ESTIMATE_CAPS: Final = EstimateCaps(
    page_input_tokens=2_733,
    page_output_tokens=8_192,
    figure_input_tokens=2_711,
    figure_output_tokens=1_024,
    embed_tokens_per_local_token=1.0,
)

#: The share of the pages read (percent, rounded up) the fallback model is
#: expected to read again.
FALLBACK_SHARE_PERCENT: Final = 10


@dataclass(frozen=True)
class _Average:
    value: float
    from_catalog: bool


def _caps_average(model_key: str | None, field: str) -> _Average:
    if model_key is not None:
        provider, model = split_model_key(model_key)
        entry = catalog_entry(provider, model) if provider is not None else None
        caps = entry.estimate_caps if entry is not None else None
        value = getattr(caps, field) if caps is not None else None
        if value is not None:
            return _Average(float(value), from_catalog=True)
    return _Average(float(getattr(DEFAULT_ESTIMATE_CAPS, field)), from_catalog=False)


def _basis(*averages: _Average) -> EstimateBasis:
    if all(average.from_catalog for average in averages):
        return EstimateBasis.CATALOG_AVERAGE
    return EstimateBasis.DEFAULT_AVERAGE


@dataclass(frozen=True)
class _Recognition:
    """The models a document's scanned pages are read with, as the ingest
    will resolve them; ``None`` keys: not configured / not usable."""

    primary: str | None
    fallback: str | None
    max_pages: int
    configured: bool


@dataclass
class _Lines:
    items: list[EstimateItem]
    notes: set[EstimateNote]


class IngestEstimator:
    """Estimates a claimed document's ingest from its local parse.

    ``settings`` / ``recognizer`` are the ingest's own (the platform's
    recognition settings — figure model included — and the scanned-page
    recognizer): the estimate counts with the models the ingest will use;
    without ``settings`` figures are not counted. ``prices`` ``None``:
    everything is unpriced."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        settings: RecognitionSettingsSource | None = None,
        recognizer: DocumentRecognizer | None = None,
        prices: PriceSource | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._recognizer = recognizer
        self._prices = prices

    @property
    def prices(self) -> PriceSource | None:
        return self._prices

    async def estimate(
        self,
        claim: ClaimedIngestion,
        local: LocalParse,
        *,
        embedding_model: str | None,
        now: datetime,
    ) -> IngestEstimate:
        """The estimate (a new id) of ingesting ``claim``'s bytes, parsed as
        ``local``, with ``embedding_model`` (the key the ingest pins).

        :raises DocumentTooLargeError: the text is over the per-document cap.
        :raises DocumentTooManyChunksError: it cuts into too many chunks."""
        bytes_unchanged = (
            claim.serving_sha256 is not None and claim.serving_sha256 == claim.content_sha256
        )
        scanned = local.kind == "pdf" and local.analysis is not None and local.analysis.ocr_pages
        recognition = await self._recognition(claim) if scanned else None
        pages = (
            await self._pages(claim, local, recognition, bytes_unchanged)
            if recognition is not None
            else None
        )
        figure_model, figure_count = await self._figures_of(claim, local, bytes_unchanged)
        embedding = await count_embedding(
            self._store,
            tenant_id=claim.tenant_id,
            document_id=claim.document_id,
            text=embedding_text(local),
            plain_text=is_plain_text_file(claim.filename),
            doc_title=knowledge_document_title(claim.filename),
            chunk_max_tokens=claim.chunk_max_tokens,
            chunk_overlap_tokens=claim.chunk_overlap_tokens,
            embedding_model=embedding_model,
        )
        lines = _Lines(items=[], notes=set())
        page_text = await self._recognition_lines(lines, pages, recognition)
        figure_text = await self._figure_lines(
            lines, claim.filename, local, figure_model, figure_count
        )
        await self._embedding_line(
            lines, embedding, embedding_model, page_text + figure_text, claim.chunk_max_tokens
        )
        if any(item.basis is EstimateBasis.DEFAULT_AVERAGE for item in lines.items):
            lines.notes.add(EstimateNote.DEFAULT_AVERAGE)
        if any(not item.priced for item in lines.items):
            lines.notes.add(EstimateNote.UNPRICED)
        reads = pages is not None and pages.within > 0
        models = EstimateModels(
            embedding=embedding_model,
            recognition=recognition.primary if recognition is not None and reads else None,
            fallback=recognition.fallback if recognition is not None and reads else None,
            figure=figure_model if figure_count is not None and figure_count.kept else None,
        )
        items = tuple(lines.items)
        return IngestEstimate(
            id=uuid4(),
            origin=EstimateOrigin(claim.origin or EstimateOrigin.REINGEST.value),
            created_at=now,
            allow_duplicate=claim.allow_duplicate,
            total_micros=sum(item.micros for item in items),
            unpriced=any(not item.priced for item in items),
            notes=tuple(sorted(note.value for note in lines.notes)),
            counts=EstimateCounts(
                chunks=embedding.chunks + _extra_chunks(page_text + figure_text, claim),
                scanned_pages=pages.scanned if pages is not None else 0,
                figures=figure_count.kept if figure_count is not None else 0,
                pages_over_limit=pages.over if pages is not None else 0,
            ),
            models=models,
            models_fingerprint=models.fingerprint(),
            items=items,
        )

    async def models_changed(self, claim: ClaimedIngestion, confirmed: EstimateModels) -> bool:
        """§14 ④ during a confirmed ingest: the recognition or figure model it
        would call now is not the one in the estimate the user confirmed.
        Only the models the estimate counted on are compared.

        Recognition is compared on the configured models, not the usable
        ones (B2b review): a confirmed model whose key cannot be read right
        now is still the one configured — a passing lookup failure is not a
        switch. A switch is a confirmed model no longer configured (or in
        another order), or a model it did not count on that would be called
        now (configured, and usable). Pages may then run on the confirmed
        fallback and cost more than estimated: by the 10-27 decision an
        actual above its estimate does not pause the ingest (both are shown)."""
        if confirmed.recognition is not None:
            if self._recognizer is None:
                return True
            plan = await self._recognizer.plan(tenant_id=claim.tenant_id)
            counted = tuple(
                key for key in (confirmed.recognition, confirmed.fallback) if key is not None
            )
            configured = [engine.choice.key for engine in plan.engines]
            if tuple(key for key in configured if key in counted) != counted:
                return True
            if any(
                engine.client is not None
                for engine in plan.engines
                if engine.choice.key not in counted
            ):
                return True
        if confirmed.figure is not None and self._settings is not None:
            settings = await self._settings.recognition_settings()
            figure = settings.figure.key if settings.figure is not None else None
            if figure != confirmed.figure:
                return True
        return False

    # -- counts ----------------------------------------------------------------

    async def _pages(
        self,
        claim: ClaimedIngestion,
        local: LocalParse,
        recognition: _Recognition,
        bytes_unchanged: bool,
    ) -> PageCount | None:
        if local.analysis is None:
            return None
        previous = (
            await self._store.list_document_pages(
                tenant_id=claim.tenant_id, document_id=claim.document_id
            )
            if bytes_unchanged
            else None
        )
        return count_pages(
            local.analysis,
            max_pages=recognition.max_pages,
            previous=previous or [],
            bytes_unchanged=bytes_unchanged,
        )

    async def _recognition(self, claim: ClaimedIngestion) -> _Recognition:
        if self._recognizer is None:
            return _Recognition(primary=None, fallback=None, max_pages=0, configured=False)
        plan = await self._recognizer.plan(tenant_id=claim.tenant_id)
        usable = [engine.choice.key for engine in plan.engines if engine.client is not None]
        return _Recognition(
            primary=usable[0] if usable else None,
            fallback=usable[1] if len(usable) > 1 else None,
            max_pages=plan.max_pages,
            configured=bool(plan.engines),
        )

    async def _figures_of(
        self, claim: ClaimedIngestion, local: LocalParse, bytes_unchanged: bool
    ) -> tuple[str | None, FigureCount | None]:
        extraction = local.office_figures if local.kind == "office" else local.pdf_figures
        if not local.figures_on or extraction is None or self._settings is None:
            return None, None
        settings = await self._settings.recognition_settings()
        if settings.figure is None:
            return None, None
        document = await self._store.get_document(
            tenant_id=claim.tenant_id, document_id=claim.document_id
        )
        count = await count_figures(
            self._store,
            tenant_id=claim.tenant_id,
            document_id=claim.document_id,
            extraction=extraction,
            previous=document.figure_stats if document is not None else None,
            bytes_unchanged=bytes_unchanged,
        )
        return settings.figure.key, count

    # -- lines ---------------------------------------------------------------------

    async def _recognition_lines(
        self, lines: _Lines, pages: PageCount | None, recognition: _Recognition | None
    ) -> int:
        """The recognition lines; the tokens of text they will read (embedded later)."""
        if pages is None or recognition is None:
            return 0
        if pages.over:
            lines.notes.add(EstimateNote.OVER_PAGE_LIMIT)
        if recognition.primary is None:
            # Not configured / no key: the pages wait, nothing is billed.
            lines.notes.add(EstimateNote.RECOGNITION_UNAVAILABLE)
            lines.items.append(
                EstimateItem(
                    kind=EstimateKind.RECOGNITION,
                    units=pages.within,
                    unit=EstimateUnit.PAGES,
                    reused_units=pages.reused,
                )
            )
            return 0
        if pages.to_read:
            lines.notes.add(EstimateNote.TEXT_LIMIT_MAY_STOP_EARLY)
        read_in = _caps_average(recognition.primary, "page_input_tokens")
        read_out = _caps_average(recognition.primary, "page_output_tokens")
        lines.items.append(
            await self._item(
                EstimateKind.RECOGNITION,
                EstimateUnit.PAGES,
                units=pages.within,
                reused=pages.reused,
                model=recognition.primary,
                input_tokens=math.ceil(pages.to_read * read_in.value),
                output_tokens=math.ceil(pages.to_read * read_out.value),
                basis=_basis(read_in, read_out),
            )
        )
        if recognition.fallback is not None and pages.to_read:
            again = math.ceil(pages.to_read * FALLBACK_SHARE_PERCENT / 100)
            back_in = _caps_average(recognition.fallback, "page_input_tokens")
            back_out = _caps_average(recognition.fallback, "page_output_tokens")
            lines.items.append(
                await self._item(
                    EstimateKind.RECOGNITION,
                    EstimateUnit.PAGES,
                    units=again,
                    reused=0,
                    model=recognition.fallback,
                    input_tokens=math.ceil(again * back_in.value),
                    output_tokens=math.ceil(again * back_out.value),
                    basis=_basis(back_in, back_out),
                )
            )
        return math.ceil(pages.to_read * read_out.value)

    async def _figure_lines(
        self,
        lines: _Lines,
        filename: str,
        local: LocalParse,
        figure_model: str | None,
        figures: FigureCount | None,
    ) -> int:
        """The figure line; the tokens of the descriptions (embedded later)."""
        if figures is None or figure_model is None:
            if not local.figures_on and _may_hold_figures(filename, local):
                lines.notes.add(EstimateNote.FIGURES_OFF)
            return 0
        if local.kind == "pdf" and local.analysis is not None and local.analysis.ocr_pages:
            lines.notes.add(EstimateNote.SCANNED_FIGURES_NOT_COUNTED)
        if not figures.kept:
            return 0
        lines.notes.add(EstimateNote.BATCH_REPEATS_COUNTED)
        lines.notes.add(EstimateNote.FIGURE_EXTRA_CALLS_NOT_COUNTED)
        describe_in = _caps_average(figure_model, "figure_input_tokens")
        describe_out = _caps_average(figure_model, "figure_output_tokens")
        lines.items.append(
            await self._item(
                EstimateKind.FIGURE,
                EstimateUnit.FIGURES,
                units=figures.kept,
                reused=figures.reused,
                model=figure_model,
                input_tokens=math.ceil(figures.to_describe * describe_in.value),
                output_tokens=math.ceil(figures.to_describe * describe_out.value),
                basis=_basis(describe_in, describe_out),
            )
        )
        return math.ceil(figures.to_describe * describe_out.value)

    async def _embedding_line(
        self,
        lines: _Lines,
        embedding: EmbeddingCount,
        model: str | None,
        produced_tokens: int,
        chunk_max_tokens: int,
    ) -> None:
        """Chunks and topic-detection blocks counted locally, converted to the
        model's tokens; plus the text recognition and descriptions will
        produce (in the reading models' tokens: about the same), embedded as
        chunks and — when topic shifts are on — once more as blocks."""
        ratio = _caps_average(model, "embed_tokens_per_local_token")
        produced = produced_tokens * (2 if semantic_breaks_enabled() else 1)
        lines.items.append(
            await self._item(
                EstimateKind.EMBEDDING,
                EstimateUnit.CHUNKS,
                units=embedding.chunks + math.ceil(produced_tokens / chunk_max_tokens),
                reused=embedding.reused,
                model=model,
                input_tokens=math.ceil(embedding.local_tokens * ratio.value) + produced,
                output_tokens=0,
                basis=EstimateBasis.LOCAL_COUNT if ratio.from_catalog else _basis(ratio),
            )
        )

    async def _item(
        self,
        kind: EstimateKind,
        unit: EstimateUnit,
        *,
        units: int,
        reused: int,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        basis: EstimateBasis,
    ) -> EstimateItem:
        provider, name = split_model_key(model) if model is not None else (None, "")
        micros = (
            await price_tokens(
                self._prices,
                provider=provider,
                model=name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            if model is not None
            else None
        )
        return EstimateItem(
            kind=kind,
            units=units,
            unit=unit,
            reused_units=reused,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            micros=micros or 0,
            basis=basis,
            priced=micros is not None or (input_tokens == 0 and output_tokens == 0),
        )


def _extra_chunks(produced_tokens: int, claim: ClaimedIngestion) -> int:
    """Chunks the text produced by recognition / descriptions will add."""
    return math.ceil(produced_tokens / claim.chunk_max_tokens)


def _may_hold_figures(filename: str, local: LocalParse) -> bool:
    """A PDF with images on its pages, or a Word / slide file (whose figures
    are only taken out while figure description is on)."""
    if local.kind == "pdf" and local.analysis is not None:
        return any(page.images for page in local.analysis.pages)
    return PurePosixPath(filename).suffix.lower() in FIGURE_OFFICE_EXTENSIONS
