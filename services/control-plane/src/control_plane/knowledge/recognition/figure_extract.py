"""B-209 PR-2 §5.4 —— 从 Word / 幻灯片里取出图片(在解析进程池里跑;只有可 pickle 的入参 /
返回值),以及各格式共用的 :class:`FigureExtraction`。

* **docx / pptx**:MarkItDown ``keep_data_uris=True``,把每个 ``![…](data:…)`` 抽成独立图片
  (WeKnora ``MarkdownImageBase64`` 同款),正文里换成图片占位符 —— 顺带修掉 Word / PPT 的
  图片占位符垃圾段。同一张图(字节相同)引用几次算几次「出现」。WMF / EMF / SVG 解不开:跳过、
  计数。页眉页脚、母版里的图 MarkItDown 本来就不输出。图片的替代文字不保留(多是形状名或自动
  生成的套话;图被描述了,描述比它好)。
* PDF 文字页上的插图见 :mod:`~.figure_pdf`,扫描页上的见 :mod:`~.figure_scan`。

单文档的过滤(§5.4 第 1~4 道)在这里做:没过的图不带字节出进程,只计数。先只看图片头里的尺寸
和字节数筛、按面积留一个短名单,只解码短名单里的图。

**内存上限**(安全复核 S-H2):``keep_data_uris`` 会把每张图的字节以 base64 展开进正文。交给
MarkItDown 之前先看压缩包里每个图片条目声明的解压后大小:单张超过 :data:`MAX_IMAGE_BYTES`、
或合计超过 :data:`MAX_OFFICE_MEDIA_BYTES`(从最大的开始)的,换成一个 1 像素的替身,计作
``unsupported``。压缩包条目声明的大小就是读出来的上限(``zipfile`` 读到声明的长度为止并校验)。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from PIL import Image

from control_plane.knowledge.parsing import (
    find_data_uri_pictures,
    office_markdown,
    scrub_data_uris,
    strip_lone_surrogates,
)
from control_plane.knowledge.recognition import figure_images
from control_plane.knowledge.recognition.figure_filters import (
    MAX_CANDIDATES,
    FigureCandidate,
    first_failed_filter,
    largest_first,
)
from control_plane.knowledge.recognition.figure_markup import inline_placeholder, placeholder

#: A single embedded picture larger than this (bytes) is not read.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
#: The pictures of one Word / slide file read at most this much together
#: (the largest are left out first) — they are base64-expanded in memory.
MAX_OFFICE_MEDIA_BYTES = 40 * 1024 * 1024
#: Where a Word / slide package keeps its pictures.
_MEDIA_DIRS = ("word/media/", "ppt/media/")


def _stand_in() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1, 1), (1, 2, 3)).save(out, format="PNG")
    return out.getvalue()


#: What an oversized picture is replaced by before MarkItDown reads the file.
_STAND_IN = _stand_in()


@dataclass(frozen=True)
class FigureExtraction:
    """A document's figures after the per-document filters: the candidates
    (with bytes) and how many images were found / dropped on the way."""

    candidates: tuple[FigureCandidate, ...] = ()
    extracted: int = 0
    filtered: int = 0
    unsupported: int = 0
    #: Past :data:`~.figure_filters.MAX_CANDIDATES` in the parse process.
    capped: int = 0

    def __add__(self, other: FigureExtraction) -> FigureExtraction:
        return FigureExtraction(
            candidates=(*self.candidates, *other.candidates),
            extracted=self.extracted + other.extracted,
            filtered=self.filtered + other.filtered,
            unsupported=self.unsupported + other.unsupported,
            capped=self.capped + other.capped,
        )

    @property
    def next_number(self) -> int:
        return max((c.number for c in self.candidates), default=0) + 1


NO_FIGURES = FigureExtraction()


@dataclass(frozen=True)
class OfficeParse:
    """A Word / slide file's Markdown with figure placeholders, and its figures."""

    markdown: str
    figures: FigureExtraction


def capped(candidates: Sequence[FigureCandidate], extraction: FigureExtraction) -> FigureExtraction:
    """``extraction`` holding at most :data:`MAX_CANDIDATES` of ``candidates``
    (largest first); the rest are counted as capped."""
    kept = largest_first(candidates, MAX_CANDIDATES)
    return replace(
        extraction,
        candidates=tuple(kept),
        capped=extraction.capped + len(candidates) - len(kept),
    )


def parse_office_figures(raw: bytes, ext: str) -> OfficeParse:
    """A ``.docx`` / ``.pptx`` parsed with its pictures pulled out (see module
    doc). The text is what :func:`~control_plane.knowledge.parsing.parse_document`
    gives, plus a placeholder where each kept picture was.

    :raises DocumentParseError: as ``parse_document`` (the empty check is the
        caller's: a picture-only file may still get its descriptions)."""
    bounded, skipped = bound_office_media(raw)
    markdown = office_markdown(bounded, ext)
    matches = find_data_uri_pictures(markdown)
    blobs: dict[str, tuple[bytes, str]] = {}
    references: dict[str, int] = {}
    in_text: list[str] = []  # each reference's image, in text order
    for match in matches:
        decoded = _decode(match.data)
        digest = hashlib.sha256(decoded).hexdigest() if decoded is not None else ""
        if decoded != _STAND_IN:  # counted by the bound already
            blobs.setdefault(digest, (decoded or b"", match.mime))
            references[digest] = references.get(digest, 0) + 1
        in_text.append(digest)
    extraction, numbers = _office_candidates(blobs, references)
    extraction = replace(
        extraction,
        extracted=extraction.extracted + skipped,
        unsupported=extraction.unsupported + skipped,
    )
    kept = {c.number for c in extraction.candidates}
    edits: list[tuple[int, int, str]] = []
    after_table: dict[int, list[int]] = {}
    for match, digest in zip(matches, in_text, strict=True):
        found = numbers.get(digest)
        number = found if found is not None and found in kept else None
        line_start = markdown.rfind("\n", 0, match.start) + 1
        if markdown[line_start : match.start].lstrip().startswith("|"):
            # A table row: an in-line reference in the cell, the block after the table.
            cell = inline_placeholder(number) if number is not None else ""
            edits.append((match.start, match.end, cell))
            if number is not None:
                spots = after_table.setdefault(_table_end(markdown, line_start), [])
                if number not in spots:
                    spots.append(number)
        else:
            block = placeholder(number) if number is not None else ""
            edits.append((match.start, match.end, block))
    for end, figures in after_table.items():
        edits.append((end, end, "".join(placeholder(n) for n in figures)))
    text = _apply(markdown, edits)
    return OfficeParse(markdown=strip_lone_surrogates(scrub_data_uris(text)), figures=extraction)


def bound_office_media(raw: bytes) -> tuple[bytes, int]:
    """``raw`` with every picture over the limits (see module doc) replaced
    by a 1-pixel stand-in, and how many were replaced. A file that is not a
    readable zip is returned as it is (MarkItDown reports it)."""
    try:
        package = zipfile.ZipFile(io.BytesIO(raw))
        entries = package.infolist()
    except (zipfile.BadZipFile, ValueError):
        return raw, 0
    media = [entry for entry in entries if entry.filename.startswith(_MEDIA_DIRS)]
    over = {entry.filename for entry in media if entry.file_size > MAX_IMAGE_BYTES}
    total = sum(entry.file_size for entry in media if entry.filename not in over)
    for entry in sorted(media, key=lambda e: -e.file_size):
        if total <= MAX_OFFICE_MEDIA_BYTES:
            break
        if entry.filename not in over:
            over.add(entry.filename)
            total -= entry.file_size
    if not over:
        return raw, 0
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bounded:
        for entry in entries:
            data = _STAND_IN if entry.filename in over else package.read(entry)
            bounded.writestr(entry, data, compress_type=entry.compress_type)
    return out.getvalue(), len(over)


def _office_candidates(
    blobs: Mapping[str, tuple[bytes, str]], references: Mapping[str, int]
) -> tuple[FigureExtraction, dict[str, int]]:
    """Filters 1-4 on what the image headers say, a shortlist of the largest
    (:data:`MAX_CANDIDATES`), and only then the decoding of the shortlist;
    candidates numbered in document order."""
    passing: dict[str, int] = {}  # digest → pixel area
    filtered = unsupported = 0
    for digest, (blob, mime) in blobs.items():
        size = (
            None
            if not blob or figure_images.is_vector_format(blob, mime)
            else (figure_images.image_size(blob))
        )
        if size is None:
            unsupported += 1
        elif first_failed_filter(
            width=size[0], height=size[1], size_bytes=len(blob), occurrences=references[digest]
        ):
            filtered += 1
        else:
            passing[digest] = size[0] * size[1]
    shortlist = set(sorted(passing, key=lambda d: -passing[d])[:MAX_CANDIDATES])
    candidates: list[FigureCandidate] = []
    numbers: dict[str, int] = {}
    for digest in blobs:  # insertion order = document order
        if digest not in shortlist:
            continue
        image = figure_images.normalize(blobs[digest][0])
        if image is None:
            unsupported += 1
            continue
        numbers[digest] = len(candidates) + 1
        candidates.append(
            FigureCandidate(
                number=len(candidates) + 1,
                image=image.jpeg,
                width=image.width,
                height=image.height,
                pixel_hash=image.pixel_hash,
            )
        )
    extraction = FigureExtraction(
        candidates=tuple(candidates),
        extracted=len(blobs),
        filtered=filtered,
        unsupported=unsupported,
        capped=len(passing) - len(shortlist),
    )
    return extraction, numbers


def _table_end(markdown: str, line_start: int) -> int:
    """The end (before its line break) of the last row of the table holding
    the line that starts at ``line_start``."""
    end = markdown.find("\n", line_start)
    while end != -1:
        following = markdown.find("\n", end + 1)
        row = markdown[end + 1 : following if following != -1 else len(markdown)]
        if not row.lstrip().startswith("|"):
            return end
        end = following
    return len(markdown)


def _apply(text: str, edits: list[tuple[int, int, str]]) -> str:
    """``text`` with non-overlapping ``(start, end, replacement)`` edits."""
    parts: list[str] = []
    position = 0
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1])):
        parts.append(text[position:start])
        parts.append(replacement)
        position = end
    parts.append(text[position:])
    return "".join(parts)


def _decode(data: str) -> bytes | None:
    if len(data) > MAX_IMAGE_BYTES * 4 // 3 + 16:
        return None
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError):
        return None
