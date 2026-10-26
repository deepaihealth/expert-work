"""Document parsing for the J.5 knowledge ingestion pipeline.

An uploaded file is converted to Markdown text — the form the chunker
consumes. PDFs go through pymupdf4llm (high-quality, heading-aware);
office formats (docx / pptx) and a PDF fallback go through
MarkItDown; xlsx is read cell by cell as displayed (B-206 K-18,
:mod:`~control_plane.knowledge.office_xlsx`), MarkItDown only when that
cannot read the file. A PDF whose pymupdf4llm parse is suspiciously sparse (a
scanned / image-only PDF) is retried with MarkItDown's extractor.
Plain text (md / txt) that is valid UTF-8 is used as decoded; a UTF-8 CSV
is handed to MarkItDown with its charset (B-181). Only other bytes get
MarkItDown's charset guess.
See ``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

import bisect
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pymupdf4llm
from markitdown import MarkItDown, StreamInfo

from control_plane.knowledge.office_docx import FIGURE_TOKEN, ConvertImage, docx_markdown
from control_plane.knowledge.office_xlsx import xlsx_markdown
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

#: B-209 PR-2 —— Word / slide files whose pictures the figure step reads:
#: only then does MarkItDown keep them inline (:func:`office_markdown`).
#: Otherwise they are dropped — never left as placeholder markup
#: (``![](data:image/png;base64...)`` / ``![Picture 2](Picture2.jpg)``) that
#: ends up a junk chunk.
FIGURE_OFFICE_EXTENSIONS = frozenset({".docx", ".pptx"})

#: An HTML comment — MarkItDown marks each slide with one; it is no content.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: Longest alt text looked back over for a picture's ``![``.
_MAX_ALT_CHARS = 2000
#: After ``](``: a data URI MarkItDown kept — ``data:<mime>;base64,<data>``,
#: optionally a ``"title"``, then ``)``. Anchored and linear (base64 holds no
#: ``)``; the whitespace runs are bounded).
_DATA_URI_TAIL = re.compile(
    r"\s{0,10}data:(?P<mime>[\w.+/-]{0,100})(?:;[\w=.+-]{1,100}){0,5},"
    r'(?P<data>[A-Za-z0-9+/=\s]*)(?:\s{1,10}"[^"\n]{0,500}")?\s{0,10}\)'
)
#: After ``](``: a data URI MarkItDown cut short (``data:image/png;base64...``).
_TRUNCATED_URI_TAIL = re.compile(r"\s{0,10}data:[\w.+/;=-]{0,200}\.\.\.\)")
#: After ``](``: the made-up file name MarkItDown gives a slide picture.
_SLIDE_PICTURE_TAIL = re.compile(r"\w{0,200}\.jpg\)")
#: A base64 payload of a data URI that escaped every pattern above.
_BASE64_PAYLOAD = re.compile(
    r"(data:[\w.+/-]{0,100}(?:;[\w=.+-]{1,100}){0,5};base64,)[A-Za-z0-9+/=]{64,}"
)


@dataclass(frozen=True)
class MarkdownPicture:
    """A Markdown image ``![alt](…)`` found in MarkItDown's output:
    ``text[start:end]``; for a kept data URI its type and base64 data."""

    start: int
    end: int
    mime: str = ""
    data: str = ""


#: After ``](``: a Word picture's token (:mod:`~.office_docx`) — its id, or ``skipped``.
_FIGURE_TOKEN_TAIL = re.compile(rf"{FIGURE_TOKEN}(?P<id>\d{{1,6}}|skipped)\)")


def find_token_pictures(text: str) -> list[MarkdownPicture]:
    """The Word pictures the converter left as tokens, in text order
    (``data``: the picture's id, or ``skipped``)."""
    return [
        MarkdownPicture(start, match.end(), "", match.group("id"))
        for start, match in _find_pictures(text, f"]({FIGURE_TOKEN}", _FIGURE_TOKEN_TAIL)
    ]


def find_data_uri_pictures(text: str) -> list[MarkdownPicture]:
    """The pictures MarkItDown kept inline as data URIs, in text order."""
    return [
        MarkdownPicture(start, match.end(), match.group("mime") or "", match.group("data"))
        for start, match in _find_pictures(text, "](data:", _DATA_URI_TAIL)
    ]


def _find_pictures(text: str, key: str, tail: re.Pattern[str]) -> list[tuple[int, re.Match[str]]]:
    """``![alt](<tail>`` pictures: each ``key`` found by plain search, the
    nearest ``![`` before it (at most :data:`_MAX_ALT_CHARS` back, same line)
    found by bisection over the ``![`` / line-break positions, the rest
    matched by the anchored ``tail`` — O(n log n) whatever the alt texts hold
    (brackets included)."""
    openers = [match.start() for match in re.finditer(r"!\[", text)]
    breaks = [match.start() for match in re.finditer("\n", text)]
    found: list[tuple[int, re.Match[str]]] = []
    position = 0
    while (index := text.find(key, position)) != -1:
        position = index + len(key)
        nearest = bisect.bisect_left(openers, index) - 1
        if nearest < 0:
            continue
        start = openers[nearest]
        line_break = bisect.bisect_left(breaks, index) - 1
        if index - start > _MAX_ALT_CHARS or (line_break >= 0 and breaks[line_break] > start):
            continue
        match = tail.match(text, index + 2)
        if match is not None and (not found or start >= found[-1][1].end()):
            found.append((start, match))
            position = match.end()
    return found


def strip_picture_markup(text: str) -> str:
    """``text`` without the picture markup MarkItDown leaves when it does not
    keep the pictures (a cut-short data URI, a slide picture's made-up file
    name) and without any base64 payload that escaped the patterns."""
    pictures = [
        *(_find_pictures(text, "](data:", _TRUNCATED_URI_TAIL)),
        *(_find_pictures(text, "](", _SLIDE_PICTURE_TAIL)),
        *(_find_pictures(text, f"]({FIGURE_TOKEN}", _FIGURE_TOKEN_TAIL)),
    ]
    parts: list[str] = []
    position = 0
    for start, match in sorted(pictures, key=lambda found: found[0]):
        if start < position:
            continue
        parts.append(text[position:start])
        position = match.end()
    parts.append(text[position:])
    return scrub_data_uris("".join(parts))


def scrub_data_uris(text: str) -> str:
    """``text`` with the payload of any base64 data URI left in it removed
    (a picture whose markup none of the patterns read) — base64 never
    reaches a chunk."""
    return _BASE64_PAYLOAD.sub(r"\1", text)


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
        if ext in FIGURE_OFFICE_EXTENSIONS:
            parsed = strip_picture_markup(parsed)
    markdown = strip_lone_surrogates(parsed)
    if not markdown.strip() or (ext in FIGURE_OFFICE_EXTENSIONS and office_text_is_empty(markdown)):
        msg = f"document {filename!r} parsed to empty content (a scanned PDF?)"
        raise DocumentParseError(msg)
    return markdown


def office_text_is_empty(markdown: str) -> bool:
    """A Word / slide file's Markdown holds nothing but MarkItDown's slide
    markers (``<!-- Slide number: 1 -->``) — a picture-only deck (B-209 PR-2)."""
    return not _HTML_COMMENT.sub("", markdown).strip()


def is_plain_text_file(filename: str) -> bool:
    """A ``.txt`` file — chunked as plain text, not as Markdown (B-199)."""
    return Path(filename).suffix.lower() == ".txt"


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


def office_markdown(raw: bytes, ext: str, *, docx_pictures: ConvertImage | None = None) -> str:
    """MarkItDown's Markdown of a Word / slide file for the figure step: a
    Word file's pictures as tokens ``docx_pictures`` hands out
    (:func:`find_token_pictures`), a slide file's inline as data URIs
    (:func:`find_data_uri_pictures` — the caller bounds them first).

    :raises DocumentParseError: MarkItDown cannot read the file."""
    if ext == ".docx" and docx_pictures is not None and _is_zip(raw):
        return _parse_docx(raw, docx_pictures)
    return _parse_with_markitdown(raw, ext, keep_data_uris=True)


def _is_zip(raw: bytes) -> bool:
    """Whether ``raw`` is a zip archive (what zipfile — and so the Word
    converter — opens, a zip behind a prefix included). A file merely named
    ``.docx`` (HTML, plain text) is left to MarkItDown's own guessing."""
    return zipfile.is_zipfile(io.BytesIO(raw))


def _parse_docx(raw: bytes, convert_image: ConvertImage | None = None) -> str:
    try:
        return docx_markdown(raw, convert_image) if convert_image else docx_markdown(raw)
    except Exception as exc:
        msg = f"MarkItDown parse failed: {exc}"
        raise DocumentParseError(msg) from exc


def _parse_with_markitdown(raw: bytes, ext: str, *, keep_data_uris: bool = False) -> str:
    if ext == ".docx" and _is_zip(raw):  # pictures never read (no figure step): office_docx
        return _parse_docx(raw)
    if ext == ".xlsx" and _is_zip(raw):
        try:
            return xlsx_markdown(raw)
        except Exception:  # noqa: S110 — MarkItDown reads it as before K-18, or fails
            pass
    # The bytes stay as they are; ``utf-8-sig`` also drops a leading BOM.
    utf8 = ext in _UTF8_HINTED_EXTENSIONS and _decode_utf8(raw) is not None
    info = StreamInfo(extension=ext, charset="utf-8-sig" if utf8 else None)
    try:
        result = MarkItDown().convert_stream(
            io.BytesIO(raw), stream_info=info, keep_data_uris=keep_data_uris
        )
    except Exception as exc:
        msg = f"MarkItDown parse failed: {exc}"
        raise DocumentParseError(msg) from exc
    return str(result.text_content or "")
