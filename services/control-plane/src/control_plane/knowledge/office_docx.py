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
"""

from __future__ import annotations

import contextlib
import io
import shutil
import zipfile
from collections.abc import Callable
from typing import Any, BinaryIO

import mammoth  # type: ignore[import-untyped]
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
        html = mammoth.convert_to_html(
            _pre_processed(file_stream),
            style_map=kwargs.get("style_map"),
            convert_image=mammoth.images.img_element(self._convert_image),
        ).value
        return self._html_converter.convert_string(html, **kwargs)


def _pre_processed(file_stream: BinaryIO) -> BinaryIO:
    """MarkItDown's ``pre_process_docx`` without reading other parts whole."""
    out = io.BytesIO()
    with zipfile.ZipFile(file_stream) as source, zipfile.ZipFile(out, "w") as target:
        target.comment = source.comment
        for entry in source.infolist():
            copy = zipfile.ZipInfo(entry.filename, entry.date_time)
            copy.compress_type, copy.external_attr = entry.compress_type, entry.external_attr
            if entry.filename in _MATH_PARTS:
                content = source.read(entry)
                with contextlib.suppress(Exception):  # as MarkItDown: keep the original
                    content = _pre_process_math(content)
                target.writestr(copy, content)
                continue
            with source.open(entry) as part, target.open(copy, "w") as written:
                shutil.copyfileobj(part, written, _COPY_CHUNK)
    out.seek(0)
    return out


def docx_markdown(raw: bytes, convert_image: ConvertImage = skip_picture) -> str:
    """The Word file's Markdown, pictures handled by ``convert_image``.

    :raises Exception: whatever MarkItDown raises for a file it cannot read."""
    converter = MarkItDown(enable_builtins=False)
    converter.register_converter(_TokenDocxConverter(convert_image))
    result = converter.convert_stream(io.BytesIO(raw), stream_info=StreamInfo(extension=".docx"))
    return str(result.text_content or "")
