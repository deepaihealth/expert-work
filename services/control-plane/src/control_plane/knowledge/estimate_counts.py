"""B-222 PR-2 §4.1 —— what an ingest will process, counted locally (no model called).

* **chunks** —— the parsed text cut with the base's parameters, as the ingest
  cuts it (:func:`~control_plane.knowledge.chunking.estimate_chunks`: topic
  shifts already in the document's break memo applied), each distinct
  retrieval text once, less those the document already has a vector for from
  the same embedding model; plus the blocks topic detection would embed;
* **pages** —— a PDF's pages decided for recognition, up to the per-document
  limit, less the ones read before (the bytes unchanged, the page on record as
  recognized: its reading is in the document's cache);
* **figures** —— the document's figures that pass the tenant-wide filter and
  the per-document cap, less the ones described before (bytes unchanged).

Over-limit text and over-limit chunk counts are refused here, before anything
is spent (``TEXT_TOO_LARGE`` / ``TOO_MANY_CHUNKS``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from control_plane.knowledge.break_cache import load_break_memo
from control_plane.knowledge.chunking import (
    count_tokens,
    estimate_chunks,
    semantic_breaks_enabled,
)
from control_plane.knowledge.parsed_size import check_parsed_size, ensure_chunk_count
from control_plane.knowledge.parsing import strip_lone_surrogates
from control_plane.knowledge.recognition.assembly import assemble_pages
from control_plane.knowledge.recognition.figure_extract import FigureExtraction
from control_plane.knowledge.recognition.figure_filters import select_figures
from control_plane.knowledge.recognition.figure_markup import resolve_figures
from control_plane.knowledge.recognition.ingest_parse import LocalParse
from control_plane.knowledge.recognition.pages import PdfAnalysis
from expert_work.persistence import KnowledgeStore
from expert_work.protocol import DocumentPage, FigureStats, PageStatus, chunk_retrieval_text


@dataclass(frozen=True)
class EmbeddingCount:
    """``chunks`` the text cuts into (distinct retrieval texts), ``reused`` of
    them already embedded by the model, and the locally counted tokens of what
    is left to embed — chunk texts and topic-detection blocks."""

    chunks: int
    reused: int
    local_tokens: int

    @property
    def to_embed(self) -> int:
        return self.chunks - self.reused


@dataclass(frozen=True)
class PageCount:
    """A PDF's scanned pages: ``within`` the per-document limit (``over``
    past it), ``reused`` of them read before."""

    scanned: int
    within: int
    over: int
    reused: int

    @property
    def to_read(self) -> int:
        return self.within - self.reused


@dataclass(frozen=True)
class FigureCount:
    """Figures to describe: ``kept`` after the filters and the cap,
    ``reused`` of them described before."""

    kept: int
    reused: int

    @property
    def to_describe(self) -> int:
        return self.kept - self.reused


def embedding_text(local: LocalParse) -> str:
    """The text the ingest will chunk, as far as it is known before any model
    runs: a PDF's text pages (scanned pages are only known once read), every
    figure placeholder dropped (descriptions are only known once written)."""
    if local.kind == "pdf" and local.analysis is not None:
        text = _pdf_text(local.analysis)
    else:
        text = local.markdown or ""
    return resolve_figures(text, {})


def _pdf_text(analysis: PdfAnalysis) -> str:
    if analysis.markdown is not None:
        return analysis.markdown
    return assemble_pages(analysis, {})


async def count_embedding(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    document_id: UUID,
    text: str,
    plain_text: bool,
    doc_title: str | None,
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    embedding_model: str | None,
) -> EmbeddingCount:
    """:class:`EmbeddingCount` of ``text``.

    :raises DocumentTooLargeError: over the per-document character cap.
    :raises DocumentTooManyChunksError: over the per-document chunk cap."""
    check_parsed_size(text)
    clean = strip_lone_surrogates(text)
    semantic = semantic_breaks_enabled()
    memo = (
        await load_break_memo(
            store, tenant_id=tenant_id, document_id=document_id, model_key=embedding_model
        )
        if semantic
        else None
    )
    estimate = estimate_chunks(
        clean,
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
        semantic=semantic,
        plain_text=plain_text,
        doc_title=doc_title,
        memo=memo,
    )
    pieces = [child for parent in estimate.parents for child in parent.children]
    ensure_chunk_count(len(pieces))
    texts: dict[str, str] = {}
    for piece in pieces:
        retrieval = chunk_retrieval_text(piece.heading_path, piece.body, doc_title)
        texts.setdefault(_digest(retrieval), retrieval)
    reused = (
        await store.chunk_hashes_on_model(
            tenant_id=tenant_id,
            document_id=document_id,
            content_hashes=list(texts),
            embedding_model=embedding_model,
        )
        if embedding_model is not None and texts
        else set()
    )
    tokens = sum(count_tokens(t) for digest, t in texts.items() if digest not in reused)
    tokens += sum(count_tokens(block) for block in estimate.break_texts)
    return EmbeddingCount(chunks=len(texts), reused=len(reused), local_tokens=tokens)


def _digest(text: str) -> str:
    """``knowledge_chunk.content_hash`` of a retrieval text (the ingest's)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def count_pages(
    analysis: PdfAnalysis,
    *,
    max_pages: int,
    previous: Sequence[DocumentPage],
    bytes_unchanged: bool,
) -> PageCount:
    """:class:`PageCount` of a PDF (``previous``: the page records of its last
    ingest)."""
    scanned = [page.index for page in analysis.ocr_pages]
    within = scanned[:max_pages]
    read_before = (
        {page.page for page in previous if page.status is PageStatus.RECOGNIZED}
        if bytes_unchanged
        else set()
    )
    reused = sum(1 for index in within if index + 1 in read_before)
    return PageCount(
        scanned=len(scanned), within=len(within), over=len(scanned) - len(within), reused=reused
    )


async def count_figures(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    document_id: UUID,
    extraction: FigureExtraction,
    previous: FigureStats | None,
    bytes_unchanged: bool,
) -> FigureCount:
    """:class:`FigureCount` of the document's figure candidates (``previous``:
    the figure counts of its last ingest)."""
    candidates = extraction.candidates
    others = (
        await store.count_figure_documents(
            tenant_id=tenant_id,
            pixel_hashes=[c.pixel_hash for c in candidates],
            exclude_document_id=document_id,
        )
        if candidates
        else {}
    )
    kept = len(select_figures(candidates, others).kept)
    described_before = previous.described if previous is not None and bytes_unchanged else 0
    return FigureCount(kept=kept, reused=min(kept, described_before))
