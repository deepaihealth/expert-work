"""Document parsing for the J.5 knowledge ingestion pipeline.

An uploaded file is converted to Markdown text — the form the chunker
consumes. PDFs go through pymupdf4llm (high-quality, heading-aware);
office formats (docx / pptx / xlsx) and a PDF fallback go through
MarkItDown. A PDF whose pymupdf4llm parse is suspiciously sparse (a
scanned / image-only PDF) is retried with MarkItDown's extractor.
See ``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pymupdf
import pymupdf4llm
from markitdown import MarkItDown

from expert_work.protocol import IngestErrorCode

#: Document extensions the knowledge pipeline accepts.
SUPPORTED_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".markdown", ".txt", ".html", ".htm", ".csv"}
)

#: A pymupdf4llm PDF parse yielding fewer than this many characters per
#: page is treated as a failed text extraction (scanned / image-only
#: PDF) and retried with MarkItDown's extractor.
_MIN_CHARS_PER_PAGE = 50

#: Lone UTF-16 surrogates. A docx that holds half of an emoji (seen in real
#: customer material) parses to a ``str`` containing one; every later UTF-8
#: encode (chunk insert, cache write) then raises ``UnicodeEncodeError`` and
#: the whole document fails with an opaque error. Dropped at the source.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


class DocumentParseError(Exception):
    """Raised when an uploaded document cannot be parsed to Markdown —
    an unsupported extension, a corrupt file, or an empty extraction
    (e.g. a scanned PDF with no text layer). ``code`` is the document's
    ``error_code``; ``str(exc)`` stays the human-readable message."""

    def __init__(self, message: str, code: IngestErrorCode = IngestErrorCode.PARSE_FAILED) -> None:
        super().__init__(message)
        self.code = code

    def __reduce__(self) -> tuple[type[DocumentParseError], tuple[str, IngestErrorCode]]:
        # Raised inside the parse process pool: the default pickling only
        # carries ``args`` and would drop ``code`` on the way back.
        return (type(self), (str(self), self.code))


def parse_document(filename: str, raw: bytes) -> str:
    """Parse an uploaded document's bytes into Markdown text.

    :raises DocumentParseError: unsupported extension, or the parse
        produced no usable text.
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        msg = f"unsupported document type: {ext or filename!r}"
        raise DocumentParseError(msg, IngestErrorCode.UNSUPPORTED_FILE)
    markdown = strip_lone_surrogates(
        _parse_pdf(raw) if ext == ".pdf" else _parse_with_markitdown(raw, ext)
    )
    if not markdown.strip():
        msg = f"document {filename!r} parsed to empty content (a scanned PDF?)"
        raise DocumentParseError(msg)
    return markdown


def strip_lone_surrogates(text: str) -> str:
    """Drop lone UTF-16 surrogates so ``text`` can always be UTF-8 encoded.
    Applied to every parser's output and to pre-parsed Markdown handed to
    the ingestion pipeline directly."""
    return _LONE_SURROGATE.sub("", text)


def _parse_pdf(raw: bytes) -> str:
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        msg = f"could not open PDF: {exc}"
        raise DocumentParseError(msg, IngestErrorCode.INVALID_FILE) from exc
    try:
        markdown: str = pymupdf4llm.to_markdown(doc)
        page_count: int = doc.page_count
    except Exception as exc:
        msg = f"PDF parse failed: {exc}"
        raise DocumentParseError(msg) from exc
    finally:
        doc.close()
    # A scanned / image-only PDF yields near-empty text from pymupdf4llm
    # — retry with MarkItDown's (pdfminer) extractor, which sometimes
    # recovers text the structured parse dropped.
    if _is_sparse(markdown, page_count):
        return _parse_with_markitdown(raw, ".pdf")
    return markdown


def _is_sparse(markdown: str, page_count: int) -> bool:
    """Whether a PDF parse looks like a failed text extraction."""
    if page_count <= 0:
        return not markdown.strip()
    return len(markdown.strip()) < _MIN_CHARS_PER_PAGE * page_count


def _parse_with_markitdown(raw: bytes, ext: str) -> str:
    try:
        result = MarkItDown().convert_stream(io.BytesIO(raw), file_extension=ext)
    except Exception as exc:
        msg = f"MarkItDown parse failed: {exc}"
        raise DocumentParseError(msg) from exc
    return str(result.text_content or "")
