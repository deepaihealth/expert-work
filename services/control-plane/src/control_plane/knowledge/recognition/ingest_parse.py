"""B-209 —— 入库时把一份上传的文件解析成 Markdown:PDF 逐页判定 + 扫描页识别(PR-1)、文档内
图片描述后插回原位(PR-2),其余格式照旧。

* **图片描述开着**才碰图片(配了能收提示词的图片描述模型且有密钥,:meth:`FigureDescriber.enabled`):
  没开时 PDF / Word / 幻灯片的解析与改前**完全一样**(图片照旧丢掉,不记图片计数、不进识别阶段)。
* **PDF**:逐页判定;扫描页送识别模型(``RECOGNIZING`` 阶段);文字页的插图另走一次进程池调用
  (有自己的时间份额,超时 / 出错只是这份文档没有插图、计为读不了,不让整份解析失败)、扫描页上
  识别出的图片区域裁出来(同样出错不连累文档);描述成引用块插回原位(同一张图只在第一次出现处
  放一次);每页的结果记在文档上(不允许静默空页),图片计数记在文档上。
* **docx / pptx**:图片抽出、描述、插回原位(表格里的图:格子里一个行内引用,引用块放在表格后);
  纯图片的文件没有可用描述时是解析失败(不是一段占位符)。
* 其余格式::func:`parse_document`。

先把这份文档的图片像素哈希(带代次栅栏)写进库,再数同租户别的文档(复核 M5;两份同时入库的
文档仍可能互相看不见 —— 剩下的竞态只是多描述一次同一个 logo)。原图在确认文档仍归自己之后才存
(带栅栏的阶段写入,复核 S-M2)。所有 PyMuPDF / MarkItDown / Pillow 的活都在解析进程池里跑。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from uuid import UUID

from control_plane.knowledge.parsed_size import (
    analyse_pdf_within_cap,
    max_parsed_chars,
    parse_office_figures_within_cap,
    parse_within_cap,
    recognized_text_limit,
)
from control_plane.knowledge.parsing import (
    FIGURE_OFFICE_EXTENSIONS,
    DocumentParseError,
    office_text_is_empty,
    parse_document,
)
from control_plane.knowledge.recognition.assembly import (
    MAX_RECOGNIZED_CHARS_PER_DOCUMENT,
    assemble_pages,
    cap_recognized_text,
    no_text_message,
    page_records,
)
from control_plane.knowledge.recognition.figure_describe import document_language
from control_plane.knowledge.recognition.figure_extract import FigureExtraction, OfficeParse
from control_plane.knowledge.recognition.figure_markup import (
    regions_to_figures,
    resolve_figures,
    resolve_in_order,
)
from control_plane.knowledge.recognition.figure_pdf import (
    NO_PDF_FIGURES,
    PdfFigures,
    collect_pdf_figures,
    place_figures,
)
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber, FigureOutcome
from control_plane.knowledge.recognition.figure_scan import ScannedFigures, crop_scanned_figures
from control_plane.knowledge.recognition.pages import PdfAnalysis, pdf_page_texts, render_pages
from control_plane.knowledge.recognition.recognizer import (
    ERROR_NOT_CONFIGURED,
    DocumentRecognizer,
    PageOutcome,
)
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import UNSET
from expert_work.protocol import FigureStats, PageDecision, PageReason, PageStatus

logger = logging.getLogger(__name__)


class PoolRunner(Protocol):
    """Runs ``fn(*args)`` in the parse process pool, on the document's budget."""

    def __call__[T](self, fn: Callable[..., T], /, *args: object) -> Awaitable[T]:
        """``fn(*args)``'s result, computed in a pool worker."""


@dataclass(frozen=True)
class ParseJob:
    """One upload being parsed. ``run_figures`` runs on the figure share of the
    budget. ``recognizing`` records the ``RECOGNIZING`` stage (fenced: raises
    once the document is no longer this worker's); ``lost`` builds the error
    raised when a fenced write is refused."""

    store: KnowledgeStore
    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    filename: str
    raw: bytes
    generation: int | None
    run: PoolRunner
    recognizing: Callable[[], Awaitable[None]]
    lost: Callable[[], Exception]
    run_figures: PoolRunner | None = None


async def parse_upload(
    job: ParseJob,
    *,
    recognizer: DocumentRecognizer | None = None,
    figures: FigureDescriber | None = None,
) -> str:
    """The upload's Markdown (see module doc).

    :raises DocumentParseError: the file cannot be parsed or holds no text.
    :raises Exception: ``job.lost()`` when a fenced write was refused."""
    ext = Path(job.filename).suffix.lower()
    describer = figures if figures is not None and await _enabled(figures, job) else None
    if ext == ".pdf":
        return await _parse_pdf(job, recognizer, describer)
    if describer is not None and ext in FIGURE_OFFICE_EXTENSIONS:
        return await _parse_office(job, describer, ext)
    return await job.run(parse_within_cap, job.filename, job.raw, max_parsed_chars())


async def _enabled(figures: FigureDescriber, job: ParseJob) -> bool:
    try:
        return await figures.enabled(tenant_id=job.tenant_id)
    except Exception:
        logger.warning("knowledge.figure_settings_failed", exc_info=True)
        return False


async def _parse_office(job: ParseJob, figures: FigureDescriber, ext: str) -> str:
    parsed: OfficeParse = await job.run(
        parse_office_figures_within_cap, job.raw, ext, max_parsed_chars()
    )
    outcome = await _describe(job, figures, parsed.figures, parsed.markdown)
    markdown = resolve_figures(parsed.markdown, outcome.blocks if outcome else {})
    if office_text_is_empty(markdown):
        stats = outcome.stats if outcome is not None else FigureStats()
        raise DocumentParseError(_no_text_message(job.filename, stats))
    return markdown


async def _parse_pdf(
    job: ParseJob, recognizer: DocumentRecognizer | None, figures: FigureDescriber | None
) -> str:
    analysis: PdfAnalysis = await job.run(
        analyse_pdf_within_cap, job.filename, job.raw, max_parsed_chars()
    )
    if not analysis.pages:  # PyMuPDF could not walk it: parsed as before, no records
        return analysis.markdown or ""
    outcomes = await _recognize(job, recognizer, analysis)
    outcome: FigureOutcome | None = None
    if figures is not None:
        analysis, outcomes, extraction = await _figures_of(job, analysis, outcomes)
        text = "\n".join([*(p.text for p in analysis.pages), *(o.text for o in outcomes.values())])
        outcome = await _describe(job, figures, extraction, text)
    blocks = outcome.blocks if outcome is not None else {}
    analysis, outcomes = _resolved(analysis, outcomes, blocks)
    pages = page_records(
        analysis,
        outcomes,
        figures_kept=outcome.kept_pages if outcome is not None else None,
        figures_described=outcome.described_pages if outcome is not None else None,
    )
    if not await job.store.set_document_pages(
        tenant_id=job.tenant_id, document_id=job.document_id, pages=pages, generation=job.generation
    ):
        raise job.lost()
    if analysis.markdown is not None:
        return analysis.markdown
    recognized = any(
        o.status is PageStatus.RECOGNIZED and o.text.strip() for o in outcomes.values()
    )
    if analysis.ocr_pages and not recognized and not blocks:
        try:
            return await job.run(parse_document, job.filename, job.raw)
        except DocumentParseError as exc:
            raise DocumentParseError(no_text_message(job.filename, pages), exc.code) from exc
    return assemble_pages(analysis, outcomes)


async def _recognize(
    job: ParseJob, recognizer: DocumentRecognizer | None, analysis: PdfAnalysis
) -> dict[int, PageOutcome]:
    """The scanned pages' outcomes (none: no page needs recognition)."""
    scanned = [page.index for page in analysis.ocr_pages]
    if not scanned:
        return {}
    await job.recognizing()
    if recognizer is None:
        pending = PageOutcome(status=PageStatus.PENDING, error=ERROR_NOT_CONFIGURED)
        return dict.fromkeys(scanned, pending)

    async def render(indexes: list[int], max_pixels: int | None) -> list[bytes]:
        return await job.run(render_pages, job.raw, indexes, max_pixels)

    # Recognition stops paying once what it read fills the room the
    # per-document text cap leaves (parsed_size.recognized_text_limit).
    limit = recognized_text_limit(analysis, MAX_RECOGNIZED_CHARS_PER_DOCUMENT)
    outcomes = await recognizer.recognize_document(
        tenant_id=job.tenant_id,
        document_id=job.document_id,
        pages=scanned,
        render=render,
        before_batch=job.recognizing,
        max_chars=limit,
    )
    return cap_recognized_text(outcomes, limit)


async def _figures_of(
    job: ParseJob, analysis: PdfAnalysis, outcomes: Mapping[int, PageOutcome]
) -> tuple[PdfAnalysis, dict[int, PageOutcome], FigureExtraction]:
    """The PDF's figures with their placeholders in place: the text pages'
    (own pool call, own time share) and the scanned pages' image regions.
    Either going wrong costs only those figures (counted unreadable)."""
    text_pages = [
        page
        for page in analysis.pages
        if page.decision is PageDecision.TEXT and page.reason is not PageReason.HIDDEN_TEXT_LAYER
    ]
    found = NO_PDF_FIGURES
    if any(page.images for page in text_pages):
        found = await _figure_call(
            job,
            collect_pdf_figures,
            job.raw,
            [page.index for page in text_pages],
            fallback=PdfFigures(
                figures=FigureExtraction(
                    extracted=sum(page.images for page in text_pages),
                    unsupported=sum(page.images for page in text_pages),
                ),
                placements={},
                blocks={},
            ),
        )
    extraction = found.figures
    if extraction.candidates:
        if analysis.markdown is not None:  # parsed whole: take each page's own text
            analysis = await job.run(pdf_page_texts, job.raw, analysis)
        texts = place_figures({page.index: page.text for page in analysis.pages}, found)
        analysis = replace(
            analysis,
            pages=tuple(
                replace(page, text=texts.get(page.index, page.text)) for page in analysis.pages
            ),
        )
    regions = {
        index: outcome.regions
        for index, outcome in outcomes.items()
        if outcome.status is PageStatus.RECOGNIZED and outcome.regions
    }
    if not regions:
        return analysis, dict(outcomes), extraction
    unreadable = sum(len(boxes) for boxes in regions.values())
    scanned: ScannedFigures = await _figure_call(
        job,
        crop_scanned_figures,
        job.raw,
        regions,
        extraction.next_number,
        fallback=ScannedFigures(
            figures=FigureExtraction(extracted=unreadable, unsupported=unreadable), numbers={}
        ),
    )
    return (
        analysis,
        {
            index: replace(
                outcome, text=regions_to_figures(outcome.text, scanned.numbers.get(index, {}))
            )
            for index, outcome in outcomes.items()
        },
        extraction + scanned.figures,
    )


async def _figure_call[T](job: ParseJob, fn: Callable[..., T], *args: object, fallback: T) -> T:
    """``fn(*args)`` in the pool on the figure share; a timeout, a crashed
    worker or any error is logged and gives ``fallback`` — figures never fail
    the document (review M7). A lost document still stops it."""
    run = job.run_figures or job.run
    try:
        return await run(fn, *args)
    except (DocumentParseError, BrokenProcessPool, Exception):
        logger.warning(
            "knowledge.figure_collect_failed document=%s step=%s",
            job.document_id,
            getattr(fn, "__name__", fn),
            exc_info=True,
        )
        return fallback


async def _describe(
    job: ParseJob, figures: FigureDescriber, extraction: FigureExtraction, text: str
) -> FigureOutcome | None:
    """The figures described (``None`` when the document has none at all)."""
    if not extraction.extracted:
        await _clear_figure_stats(job)
        return None
    hashes = tuple(c.pixel_hash for c in extraction.candidates)
    # Recorded before the tenant-wide count (review M5), fenced.
    if not await job.store.set_document_figures(
        tenant_id=job.tenant_id,
        document_id=job.document_id,
        stats=UNSET,
        pixel_hashes=hashes,
        generation=job.generation,
    ):
        raise job.lost()
    if extraction.candidates:
        await job.recognizing()  # fenced: a lost document pays for no figure
    outcome = await figures.describe(
        tenant_id=job.tenant_id,
        kb_id=job.kb_id,
        document_id=job.document_id,
        extraction=extraction,
        language=document_language(resolve_figures(text, {})),
    )
    if outcome.originals:
        await job.recognizing()  # still ours before anything goes to storage (S-M2)
        await figures.store_originals(
            tenant_id=job.tenant_id,
            kb_id=job.kb_id,
            document_id=job.document_id,
            generation=job.generation,
            originals=outcome.originals,
        )
    if not await job.store.set_document_figures(
        tenant_id=job.tenant_id,
        document_id=job.document_id,
        stats=outcome.stats,
        pixel_hashes=hashes,
        generation=job.generation,
    ):
        raise job.lost()
    return outcome


async def _clear_figure_stats(job: ParseJob) -> None:
    """No figures this time: drop counts a previous version left (and write
    nothing when there were none)."""
    document = await job.store.get_document(tenant_id=job.tenant_id, document_id=job.document_id)
    if document is None or document.figure_stats is None:
        return
    if not await job.store.set_document_figures(
        tenant_id=job.tenant_id,
        document_id=job.document_id,
        stats=None,
        pixel_hashes=(),
        generation=job.generation,
    ):
        raise job.lost()


def _resolved(
    analysis: PdfAnalysis, outcomes: Mapping[int, PageOutcome], blocks: Mapping[int, str]
) -> tuple[PdfAnalysis, dict[int, PageOutcome]]:
    """Pages and scanned-page outcomes with their placeholders resolved in
    page order — a figure's block where it first appears."""
    indexes = [page.index for page in analysis.pages]
    texts = [
        outcomes[index].text if index in outcomes else page.text
        for index, page in zip(indexes, analysis.pages, strict=True)
    ]
    resolved = dict(zip(indexes, resolve_in_order(texts, blocks), strict=True))
    pages = tuple(
        page if page.index in outcomes else replace(page, text=resolved[page.index])
        for page in analysis.pages
    )
    return (
        analysis if analysis.markdown is not None else replace(analysis, pages=pages),
        {index: replace(o, text=resolved.get(index, o.text)) for index, o in outcomes.items()},
    )


def _no_text_message(filename: str, stats: FigureStats) -> str:
    message = f"document {filename!r} has no text"
    if not stats.extracted:
        return f"{message} (a scanned file?)"
    parts = [f"{stats.extracted} pictures"]
    for count, label in (
        (stats.filtered, "filtered (too small / repeated)"),
        (stats.capped, "past the per-document limit"),
        (stats.unsupported, "in a format that cannot be read"),
        (stats.failed, "could not be described"),
        (stats.pending, "waiting for a description model"),
    ):
        if count:
            parts.append(f"{count} {label}")
    return f"{message}: {', '.join(parts)}"
