"""Word (``.docx``) to Markdown through MarkItDown, with the pictures under our
control (B-209 PR-2, fifth review round).

MarkItDown converts a Word file with mammoth, whose default picture handling
reads every referenced picture part in full and inlines it as a base64 data
URI — once per reference (one picture referenced a hundred times in a
0.44 MB upload became 48 MB of Markdown), and whatever the part's name, type
or relationship says (mammoth resolves ``r:embed`` by id only). Here mammoth
gets a ``convert_image`` of ours instead, so the bytes are only ever read
where a cap can be applied and a picture is never inlined at all: the
Markdown carries a short token (``![alt](expert-work-figure:<id>)``) the
figure step resolves, or ``expert-work-figure:skipped`` (dropped).

Only this converter is registered for the call: were it to fail, MarkItDown
would otherwise fall back to its own Word converter and its uncapped reads.

MarkItDown's pre-processing (math to LaTeX in the text parts) reads every
part of the package whole — pictures included — and writes them all out
uncompressed; here only the text parts are read whole, every other part is
copied through in chunks with its own compression.

B-206 K-19 —— Word marks a heading by a heading style or by an outline level
(on the paragraph, or on a style of another name); mammoth reads heading
styles by name only. On the dev corpus 37 of 63 Word files marked their
headings by outline level alone and all 2,310 of their chunks lost their
heading path. A body paragraph (not in a table, at most
:data:`_OUTLINE_HEADING_MAX_CHARS` characters) with outline level ``n`` gets a
style of ours that the style map turns into a level ``n + 1`` heading; a
paragraph in a heading style keeps mammoth's own mapping.
"""

from __future__ import annotations

import contextlib
import io
import re
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

import mammoth  # type: ignore[import-untyped]
from lxml import etree
from markitdown import DocumentConverterResult, MarkItDown, StreamInfo
from markitdown.converter_utils.docx.pre_process import _pre_process_math
from markitdown.converters import DocxConverter

#: The Markdown image target of a picture the figure step reads (+ its id).
FIGURE_TOKEN = "expert-work-figure:"  # noqa: S105 — a marker, not a secret
#: The target of a picture that is not read (no figure step, or over a cap).
SKIPPED_TOKEN = f"{FIGURE_TOKEN}skipped"

#: mammoth's ``convert_image`` contract: an image element in, ``<img>``
#: attributes out.
ConvertImage = Callable[[Any], dict[str, str]]

#: The parts MarkItDown pre-processes (math to LaTeX).
_MATH_PARTS = frozenset({"word/document.xml", "word/footnotes.xml", "word/endnotes.xml"})
#: Bytes per read while copying a part through.
_COPY_CHUNK = 1024 * 1024

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCUMENT_PART = "word/document.xml"
_STYLES_PART = "word/styles.xml"
#: K-19: the style ids given to outline-level paragraphs, and their mapping
#: (Markdown has six heading levels; Word's 7th to 9th read as the 6th).
_OUTLINE_STYLE = "ExpertWorkOutline"
_OUTLINE_STYLE_MAP = "\n".join(
    f"p.{_OUTLINE_STYLE}{level} => h{min(level, 6)}:fresh" for level in range(1, 10)
)
#: A paragraph longer than this keeps its outline level as body text: a
#: heading that long is a stray level on a body paragraph.
_OUTLINE_HEADING_MAX_CHARS = 100
#: Word's own heading styles, which mammoth maps by name.
_HEADING_STYLE_NAME = re.compile(r"^heading [1-9]$", re.IGNORECASE)
#: Outline level 9 is "body text".
_BODY_LEVEL = 9
#: No entities, no network, libxml2's size limits kept (a part past them is left as it is).
_SAFE_XML = etree.XMLParser(resolve_entities=False, no_network=True)


def skip_picture(image: Any) -> dict[str, str]:
    """A picture left out without reading a byte of it."""
    del image
    return {"src": SKIPPED_TOKEN}


class _TokenDocxConverter(DocxConverter):  # type: ignore[misc]
    """MarkItDown's Word converter with our ``convert_image``."""

    def __init__(self, convert_image: ConvertImage) -> None:
        super().__init__()
        self._convert_image = convert_image

    def convert(
        self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any
    ) -> DocumentConverterResult:
        extra = kwargs.get("style_map")
        html = mammoth.convert_to_html(
            _pre_processed(file_stream),
            style_map=f"{_OUTLINE_STYLE_MAP}\n{extra}" if extra else _OUTLINE_STYLE_MAP,
            convert_image=mammoth.images.img_element(self._convert_image),
            # Linked pictures (``TargetMode="External"``, ``file://…``) are never
            # opened — mammoth's default today, pinned here.
            external_file_access=False,
        ).value
        return self._html_converter.convert_string(html, **kwargs)


def _pre_processed(file_stream: BinaryIO) -> BinaryIO:
    """MarkItDown's ``pre_process_docx`` without reading other parts whole,
    outline-level paragraphs styled as headings (K-19)."""
    out = io.BytesIO()
    with zipfile.ZipFile(file_stream) as source, zipfile.ZipFile(out, "w") as target:
        target.comment = source.comment
        outline = _style_outlines(source)
        for entry in source.infolist():
            copy = zipfile.ZipInfo(entry.filename, entry.date_time)
            copy.compress_type, copy.external_attr = entry.compress_type, entry.external_attr
            if entry.filename in _MATH_PARTS:
                content = source.read(entry)
                with contextlib.suppress(Exception):  # as MarkItDown: keep the original
                    content = _pre_process_math(content)
                if entry.filename == _DOCUMENT_PART:
                    with contextlib.suppress(Exception):  # keep it as it is
                        content = _outline_headings(content, outline)
                target.writestr(copy, content)
                continue
            with source.open(entry) as part, target.open(copy, "w") as written:
                shutil.copyfileobj(part, written, _COPY_CHUNK)
    out.seek(0)
    return out


@dataclass(frozen=True)
class _StyleOutlines:
    """The styles that give an outline level (id → 0-based level, through
    ``basedOn``) and Word's own heading styles (ids)."""

    levels: dict[str, int]
    headings: frozenset[str]


def _style_outlines(package: zipfile.ZipFile) -> _StyleOutlines:
    """The package's style outlines; none when it has no readable styles part."""
    try:
        root = etree.fromstring(package.read(_STYLES_PART), parser=_SAFE_XML)
    except Exception:  # no styles part, or not XML: direct outline levels only
        return _StyleOutlines(levels={}, headings=frozenset())
    own: dict[str, int] = {}
    based_on: dict[str, str] = {}
    headings: set[str] = set()
    for style in root.iter(f"{_W}style"):
        style_id = style.get(f"{_W}styleId")
        if style_id is None or style.get(f"{_W}type", "paragraph") != "paragraph":
            continue
        if _HEADING_STYLE_NAME.match(_val(style.find(f"{_W}name")) or ""):
            headings.add(style_id)
        level = _level(style.find(f"{_W}pPr/{_W}outlineLvl"))
        if level is not None:
            own[style_id] = level
        parent = _val(style.find(f"{_W}basedOn"))
        if parent:
            based_on[style_id] = parent
    levels = {}
    for style_id in set(own) | set(based_on):
        seen, current = set(), style_id
        while current not in own and current in based_on and current not in seen:
            seen.add(current)
            current = based_on[current]
        if current in own:
            levels[style_id] = own[current]
    return _StyleOutlines(levels=levels, headings=frozenset(headings))


def _outline_headings(document: bytes, outline: _StyleOutlines) -> bytes:
    """``document`` with each body paragraph that has an outline level (its
    own, else its style's) and is short enough given our heading style of
    that level; unchanged when there is none."""
    root = etree.fromstring(document, parser=_SAFE_XML)
    changed = False
    for paragraph in root.iter(f"{_W}p"):
        level = _paragraph_level(paragraph, outline)
        if level is None:
            continue
        properties = paragraph.find(f"{_W}pPr")
        if properties is None:
            properties = etree.Element(f"{_W}pPr")
            paragraph.insert(0, properties)
        style = properties.find(f"{_W}pStyle")
        if style is None:
            style = etree.Element(f"{_W}pStyle")
            properties.insert(0, style)
        style.set(f"{_W}val", f"{_OUTLINE_STYLE}{level + 1}")
        changed = True
    if not changed:
        return document
    return bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True))


def _paragraph_level(paragraph: Any, outline: _StyleOutlines) -> int | None:
    """The paragraph's 0-based heading level, or ``None``: none, body text
    (9), a heading style, in a table, empty or too long."""
    properties = paragraph.find(f"{_W}pPr")
    style_id = _val(properties.find(f"{_W}pStyle")) if properties is not None else None
    if style_id in outline.headings:
        return None
    own = _level(properties.find(f"{_W}outlineLvl")) if properties is not None else None
    level = own if own is not None else outline.levels.get(style_id or "")
    if level is None or level >= _BODY_LEVEL:
        return None
    if any(True for _ in paragraph.iterancestors(f"{_W}tbl")):
        return None
    text = "".join(node.text or "" for node in paragraph.iter(f"{_W}t")).strip()
    return level if text and len(text) <= _OUTLINE_HEADING_MAX_CHARS else None


def _val(element: Any) -> str | None:
    return element.get(f"{_W}val") if element is not None else None


def _level(element: Any) -> int | None:
    value = _val(element)
    return int(value) if value is not None and value.isdigit() else None


def docx_markdown(raw: bytes, convert_image: ConvertImage = skip_picture) -> str:
    """The Word file's Markdown, pictures handled by ``convert_image``.

    :raises Exception: whatever MarkItDown raises for a file it cannot read."""
    converter = MarkItDown(enable_builtins=False)
    converter.register_converter(_TokenDocxConverter(convert_image))
    result = converter.convert_stream(io.BytesIO(raw), stream_info=StreamInfo(extension=".docx"))
    return str(result.text_content or "")
