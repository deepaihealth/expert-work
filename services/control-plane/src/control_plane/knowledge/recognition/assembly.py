"""B-209 §5.1 / §5.6 —— 识别完一份 PDF 之后:逐页记录、按页序拼回 Markdown、全部无字时的报错文案。

纯函数(不碰进程池、不碰网络),入库路径 ``ingestion._parse_pdf_pages`` 调用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from control_plane.knowledge.recognition.pages import PdfAnalysis
from control_plane.knowledge.recognition.recognizer import PageOutcome
from expert_work.protocol import DocumentPage, PageDecision, PageStats, PageStatus


def page_records(analysis: PdfAnalysis, outcomes: Mapping[int, PageOutcome]) -> list[DocumentPage]:
    """Every page's record (B-209 §5.6): how its text was obtained and, for a
    scanned page, what recognition made of it."""
    records = []
    for page in analysis.pages:
        if page.decision is not PageDecision.OCR:
            records.append(
                DocumentPage(
                    page=page.index + 1,
                    decision=page.decision,
                    reason=page.reason,
                    chars=page.chars,
                    images=page.images,
                )
            )
            continue
        outcome = outcomes.get(page.index) or PageOutcome(
            status=PageStatus.PENDING, error="not recognized"
        )
        records.append(
            DocumentPage(
                page=page.index + 1,
                decision=page.decision,
                reason=page.reason,
                status=outcome.status,
                provider=outcome.provider,
                model=outcome.model,
                chars=len("".join(outcome.text.split())),
                images=page.images,
                cached=outcome.cached,
                error=outcome.error,
            )
        )
    return records


def assemble_pages(analysis: PdfAnalysis, outcomes: Mapping[int, PageOutcome]) -> str:
    """The document's Markdown, page by page in order: a text page's own text,
    a scanned page's recognized text (nothing for a page not recognized)."""
    parts = []
    for page in analysis.pages:
        if page.decision is PageDecision.OCR:
            outcome = outcomes.get(page.index)
            recognized = outcome is not None and outcome.status is PageStatus.RECOGNIZED
            text = outcome.text if recognized and outcome is not None else ""
        else:
            text = page.text
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def no_text_message(filename: str, pages: Sequence[DocumentPage]) -> str:
    stats = PageStats.of(pages)
    scanned = sum(1 for page in pages if page.decision is PageDecision.OCR)
    parts = [f"document {filename!r} has no text: {scanned} of {stats.pages_total} pages scanned"]
    for count, label in (
        (stats.pages_ocr, "recognized with no text"),
        (stats.pages_failed, "failed recognition"),
        (stats.pages_pending, "waiting for recognition"),
        (stats.pages_over_limit, "past the recognition page limit"),
    ):
        if count:
            parts.append(f"{count} {label}")
    reasons = sorted({page.error for page in pages if page.error})
    message = ", ".join(parts)
    return f"{message} ({'; '.join(reasons)})" if reasons else message
