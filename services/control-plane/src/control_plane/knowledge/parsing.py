"""Document parsing for the J.5 knowledge ingestion pipeline.

An uploaded file is converted to Markdown text — the form the chunker
consumes. PDFs go through pymupdf4llm (high-quality, heading-aware);
office formats (docx / pptx / xlsx) and a PDF fallback go through
MarkItDown. A PDF whose pymupdf4llm parse is suspiciously sparse (a
scanned / image-only PDF) is retried with MarkItDown's extractor.
Plain text (md / txt) that is valid UTF-8 is used as decoded; a UTF-8 CSV
is handed to MarkItDown with its charset (B-181). Only other bytes get
MarkItDown's charset guess.
See ``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pymupdf
import pymupdf4llm
from markitdown import MarkItDown, StreamInfo

from expert_work.protocol import IngestErrorCode

#: Document extensions the knowledge pipeline accepts.
SUPPORTED_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".markdown", ".txt", ".html", ".htm", ".csv"}
)

#: A pymupdf4llm PDF parse yielding fewer than this many characters per
#: page is treated as a failed text extraction (scanned / image-only
#: PDF) and retried with MarkItDown's extractor.
_MIN_CHARS_PER_PAGE = 50

#: Lone UTF-16 surrogates, and NUL. A docx that holds half of an emoji (seen
#: in real customer material) parses to a ``str`` containing a surrogate; every
#: later UTF-8 encode (chunk insert, cache write) then raises
#: ``UnicodeEncodeError`` and the whole document fails with an opaque error.
#: A NUL fails the same way when Postgres stores the chunk. Dropped at the source.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff\x00]")

#: Plain-text formats used as decoded when the bytes are valid UTF-8 (B-181).
#: MarkItDown guesses the charset from the first 4096 bytes only: an ASCII
#: head makes it ``ascii`` and a later em dash fails the whole document, a
#: few ``¥`` signs make it a wrong legacy charset and they come back as
#: mojibake. It also strips trailing spaces, which in Markdown are a hard
#: line break.
_PLAIN_TEXT_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})

#: Formats MarkItDown still converts, told the charset when the bytes are
#: valid UTF-8 so its guess is never used for them (B-181).
_UTF8_HINTED_EXTENSIONS = frozenset({".csv"})


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
    text = _decode_utf8(raw) if ext in _PLAIN_TEXT_EXTENSIONS else None
    if ext == ".pdf":
        parsed = _parse_pdf(raw)
    elif text is not None:
        parsed = _normalise_plain_text(text)
    else:
        parsed = _parse_with_markitdown(raw, ext)
    markdown = strip_lone_surrogates(parsed)
    if not markdown.strip():
        msg = f"document {filename!r} parsed to empty content (a scanned PDF?)"
        raise DocumentParseError(msg)
    return markdown


def strip_lone_surrogates(text: str) -> str:
    """Drop lone UTF-16 surrogates and NUL so ``text`` can always be UTF-8
    encoded and stored.
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


def _decode_utf8(raw: bytes) -> str | None:
    """``raw`` decoded as UTF-8 with a leading BOM dropped, or ``None`` when it
    is not valid UTF-8 (GBK and other legacy encodings almost never are) or
    holds a NUL byte: BOM-less UTF-16 of ASCII text is valid UTF-8 with a NUL
    after every character, which the guess still decodes correctly."""
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _normalise_plain_text(text: str) -> str:
    """MarkItDown's output clean-up, except that a run of plain spaces ending a
    non-blank line is kept (a Markdown hard break): lines split on ``\\r?\\n``
    and lose other trailing whitespace, then runs of blank lines become one —
    so text that parsed the same before still chunks the same."""
    return re.sub(
        r"\n{3,}", "\n\n", "\n".join(_strip_line_end(line) for line in re.split(r"\r?\n", text))
    )


def _strip_line_end(line: str) -> str:
    """``line`` without trailing whitespace, unless that is only spaces after
    some text — a Markdown hard break, which is kept."""
    body = line.rstrip()
    tail = line[len(body) :]
    return body + tail if body and tail.strip(" ") == "" else body


def _parse_with_markitdown(raw: bytes, ext: str) -> str:
    # The bytes stay as they are; ``utf-8-sig`` also drops a leading BOM.
    utf8 = ext in _UTF8_HINTED_EXTENSIONS and _decode_utf8(raw) is not None
    info = StreamInfo(extension=ext, charset="utf-8-sig" if utf8 else None)
    try:
        result = MarkItDown().convert_stream(io.BytesIO(raw), stream_info=info)
    except Exception as exc:
        msg = f"MarkItDown parse failed: {exc}"
        raise DocumentParseError(msg) from exc
    return str(result.text_content or "")
